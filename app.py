import os
import sqlite3
import json
from datetime import datetime
from dateutil.relativedelta import relativedelta

from flask import Flask, request, jsonify, send_file
from openai import OpenAI
from dotenv import load_dotenv
from pypdf import PdfReader, PdfWriter

import requests
from bs4 import BeautifulSoup
import re

# -----------------------------
# App setup
# -----------------------------
load_dotenv() 
app = Flask(__name__)

# OpenAI will automatically read OPENAI_API_KEY from environment
##client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DB_PATH = "negotiations.db"


# -----------------------------
# Database helpers
# -----------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database with conversation history"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS negotiations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zipcode TEXT,
            current_rent INTEGER,
            initial_target_rent INTEGER,
            current_target_rent INTEGER,
            status TEXT,
            tenant_email TEXT,
            conversation_history TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_negotiation(tenant_email):
    """Get negotiation by tenant email"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM negotiations WHERE tenant_email=? ORDER BY updated_at DESC LIMIT 1",
        (tenant_email,)
    ).fetchone()
    conn.close()
    return row


def create_negotiation(tenant_name, address, city, state, zipcode, current_rent, target_rent, tenant_email):
    """Create negotiation with empty conversation history"""
    if not tenant_email:
        raise ValueError("tenant_email is required!")
    
    conn = get_db()
    conn.execute("""
        INSERT INTO negotiations
        (tenant_name, address, city, state, zipcode, current_rent, initial_target_rent,
         current_target_rent, tenant_email, conversation_history, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
    """, (
        tenant_name, address, city, state, zipcode, current_rent, target_rent,
        target_rent, tenant_email, json.dumps([]), 
        datetime.utcnow().isoformat(), datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def update_negotiation(tenant_email, **fields):
    """Update negotiation by email"""
    if not fields:
        return
    
    fields['updated_at'] = datetime.utcnow().isoformat()
    
    set_clause = ', '.join([f"{k}=?" for k in fields.keys()])
    values = list(fields.values()) + [tenant_email]
    
    conn = get_db()
    conn.execute(
        f"UPDATE negotiations SET {set_clause} WHERE tenant_email=?",
        values
    )
    conn.commit()
    conn.close()


def add_message_to_history(tenant_email, role, content):
    """Add a message to the conversation history"""
    negotiation = get_negotiation(tenant_email)
    if not negotiation:
        return
    
    history = json.loads(negotiation['conversation_history'] or '[]')
    history.append({
        "role": role,
        "content": content,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    update_negotiation(tenant_email, conversation_history=json.dumps(history))


def get_conversation_history(tenant_email):
    """Get conversation history for OpenAI format"""
    negotiation = get_negotiation(tenant_email)
    if not negotiation:
        return []
    
    history = json.loads(negotiation['conversation_history'] or '[]')
    # Return in OpenAI format (without timestamps)
    return [{"role": msg["role"], "content": msg["content"]} for msg in history]


# -----------------------------
# Zillow scraping
# -----------------------------

def get_zillow_rent_estimate(address, city, state, zipcode):
    """
    Scrape Zillow for Rent Zestimate
    Returns: estimated monthly rent or None
    """
    try:
        # Clean up address for URL
        search_query = f"{address} {city} {state} {zipcode}".strip().replace(' ', '+')
        url = f"https://www.zillow.com/homes/{search_query}_rb/"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        }
        
        print(f"\n{'='*60}")
        print(f"ZILLOW SCRAPING DEBUG")
        print(f"{'='*60}")
        print(f"Address: {address}, {city}, {state} {zipcode}")
        print(f"URL: {url}")
        
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        print(f"Response Status: {response.status_code}")
        print(f"Final URL: {response.url}")
        
        # Check for common blocking responses
        if response.status_code == 403:
            print(f"❌ Zillow blocked request (403 Forbidden)")
            return None
        
        if response.status_code == 429:
            print(f"❌ Rate limited by Zillow (429 Too Many Requests)")
            return None
            
        if response.status_code != 200:
            print(f"❌ Bad status code: {response.status_code}")
            return None
        
        # Check if we got a captcha page
        if 'captcha' in response.text.lower() or 'robot' in response.text.lower():
            print(f"❌ Zillow detected bot (captcha page)")
            return None
        
        soup = BeautifulSoup(response.content, 'html.parser')
        page_text = soup.get_text()
        
        # Debug: Show page length
        print(f"Page text length: {len(page_text)} characters")
        
        # Find ALL dollar amounts
        all_dollars = re.findall(r'\$([0-9,]+)', page_text)
        print(f"All $ amounts found: {len(all_dollars)} total")
        print(f"First 15: {all_dollars[:15]}")
        
        # Try different patterns (in order of reliability)
        patterns = {
            'Rent Zestimate': r'Rent\s+Zestimate[®™]?\s*[:\s]*\$?\s*([0-9,]+)',
            'Zestimate Rent': r'Zestimate[®™]?\s+Rent\s*[:\s]*\$?\s*([0-9,]+)',
            'Estimated rent': r'Estimated\s+rent[:\s]*\$?\s*([0-9,]+)',
            'Monthly rent': r'Monthly\s+rent[:\s]*\$?\s*([0-9,]+)',
            '/mo pattern': r'\$([0-9,]+)\s*/\s*mo',
            'rent/month': r'\$([0-9,]+)\s*/\s*month',
            'Rent: $X': r'Rent\s*:\s*\$([0-9,]+)',
        }
        
        for name, pattern in patterns.items():
            matches = re.findall(pattern, page_text, re.IGNORECASE)
            if matches:
                print(f"✓ Pattern '{name}': {matches}")
                # Take the highest reasonable rent
                rents = [int(m.replace(',', '')) for m in matches]
                reasonable = [r for r in rents if 500 <= r <= 20000]
                if reasonable:
                    rent = max(reasonable)
                    print(f"✅ FINAL RENT: ${rent}")
                    print(f"{'='*60}\n")
                    return rent
        
        # Last resort: look for any reasonable rent amount in context
        print("⚠️  Trying last resort: finding reasonable amounts near 'rent' keyword...")
        rent_context = re.findall(r'rent[^$]*\$([0-9,]+)', page_text, re.IGNORECASE)
        if rent_context:
            print(f"Found amounts near 'rent': {rent_context}")
            rents = [int(m.replace(',', '')) for m in rent_context]
            reasonable = [r for r in rents if 500 <= r <= 20000]
            if reasonable:
                rent = max(reasonable)
                print(f"✅ FINAL RENT (contextual): ${rent}")
                print(f"{'='*60}\n")
                return rent
        
        print("❌ No rent found in any pattern")
        print(f"{'='*60}\n")
        return None
        
    except requests.exceptions.Timeout:
        print(f"❌ Request timeout (15 seconds)")
        return None
    except requests.exceptions.ConnectionError:
        print(f"❌ Connection error - cannot reach Zillow")
        return None
    except Exception as e:
        print(f"❌ Error scraping Zillow: {e}")
        import traceback
        traceback.print_exc()
        return None


# -----------------------------
# AI Conversation Handler
# -----------------------------

def get_negotiation_system_prompt(negotiation):
    """Create system prompt with negotiation context"""
    full_address = f"{negotiation['address']}, {negotiation['city']}, {negotiation['state']} {negotiation['zipcode']}"
    
    return f"""You are a professional property manager named Alex conducting a rent negotiation via email.

PROPERTY CONTEXT:
- Tenant: {negotiation['tenant_name']}
- Property: {full_address}
- Current Rent: ${negotiation['current_rent']}/month
- Market Rate: ${negotiation['initial_target_rent']}/month
- Your Current Position: ${negotiation['current_target_rent']}/month

CRITICAL STRATEGY - LET THEM SPEAK FIRST:
1. **INITIAL LETTER**: DO NOT mention any specific dollar amount! Simply inform them of the upcoming renewal and invite them to discuss. Let the TENANT propose a number first.
2. **After they propose**: Now you can respond with your position and negotiate from there
3. This strategy gets their willingness to pay before anchoring them with your number

NEGOTIATION RULES:
1. Your absolute minimum is ${negotiation['current_rent']}/month (can stay at current rent if needed)
2. When tenant makes counteroffers, you can move down by up to $100 per round
3. Accept any offer at or above your current position immediately
4. If tenant insists and you're close, you can go lower to reach agreement
5. Always be warm, professional, and understanding

TONE & LENGTH:
- Keep responses BRIEF (1-2 short paragraphs maximum)
- No repetition or verbose explanations
- Direct and friendly, not formal or wordy
- Get to the point quickly

RESPONSE FORMAT:
- Write as if you're writing an email body
- Do NOT include subject lines, greetings like "Dear [Name]", or signatures
- Start directly with the content
- End naturally without "Best regards" or similar closings

CURRENT STATUS: {negotiation['status']}

Your goal is to get the best rent while maintaining a positive relationship with the tenant. Remember: LET THEM SPEAK FIRST!
"""


def negotiate_with_ai(tenant_email, tenant_message=None):
    """
    Handle the entire negotiation conversation with AI
    AI analyzes the message and makes decisions
    Returns: (letter_text, status, metadata)
    """
    negotiation = get_negotiation(tenant_email)
    if not negotiation:
        return None, "error", {"error": "No negotiation found"}
    
    # Get conversation history
    history = get_conversation_history(tenant_email)
    
    # System prompt
    system_prompt = get_negotiation_system_prompt(negotiation)
    
    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    
    # Add tenant message if provided
    if tenant_message:
        messages.append({"role": "user", "content": tenant_message})
        add_message_to_history(tenant_email, "user", tenant_message)
    
    # =================================================================
    # STEP 1: Ask AI to analyze the negotiation and make a decision
    # =================================================================
    
    # If there's no tenant message, this is the initial letter
    if not tenant_message:
        # Use template-based initial letter instead of AI generation
        print(f"📧 INITIAL LETTER (template) for {negotiation['tenant_name']}")
        print(f"   Strategy: Let tenant propose first")
        print(f"   Target rent (internal): ${negotiation['current_target_rent']}/month")
        
        # Template with placeholders
        letter_text = f"""Hi {negotiation['tenant_name']},

It's been a while since our original lease rent at ${negotiation['current_rent']}/month. The market price in the area has shifted since then.

I did some research:
- Zillow shows the current market rent for similar properties in {negotiation['city']} is around ${negotiation['initial_target_rent']}/month
- Other comparable listings in the area range from ${int(negotiation['initial_target_rent'] * 0.9)} to ${int(negotiation['initial_target_rent'] * 1.1)}/month

Can you let me know the following by the end of this month?

1. Knowing the market rent and the prices available in the open market, what price would you be comfortable with? I'd like you to name the price since I want to make sure it's not a big burden for you to come up with the rent.

2. Do you prefer a 1-year or 2-year contract? For a 2-year contract, I would expect a slightly higher rent commitment since the rent is guaranteed for 2 years.

Looking forward to hearing from you!"""
        
        # Save to history
        add_message_to_history(tenant_email, "assistant", letter_text)
        
        # Return with "countered" status
        return letter_text, "countered", {"management_offer": negotiation['current_target_rent']}
    
    # =================================================================
    # Continue with tenant message analysis for ongoing negotiation
    # =================================================================
    
    analysis_prompt = f"""
Based on the conversation, analyze the tenant's latest message and make a negotiation decision.

Current negotiation state:
- Your current position: ${negotiation['current_target_rent']}/month
- Initial target: ${negotiation['initial_target_rent']}/month
- Current rent: ${negotiation['current_rent']}/month
- Absolute minimum: ${negotiation['current_rent']}/month (can stay at current if needed)

Respond with a JSON object containing:
{{
    "tenant_offer": <number or null>,
    "tenant_intent": "accepting" | "countering" | "discussing" | "declining",
    "should_accept": <boolean>,
    "recommended_counter": <number or null>,
    "reasoning": "<brief explanation>"
}}

Rules:
- If tenant says "that works", "sounds good", "deal", etc. without a number, they're accepting your current position
- If tenant mentions a specific dollar amount, that's their offer
- should_accept = true if tenant's offer >= your current position
- If should_accept = false, recommend a counteroffer (can move down by up to $100)
- If tenant insists and you're getting close, you can go lower - minimum is ${negotiation['current_rent']}
- Be willing to compromise to reach agreement
"""
    
    try:
        # Get AI's analysis
        analysis_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages + [{"role": "user", "content": analysis_prompt}],
            temperature=0.3,  # Lower temperature for more consistent analysis
            max_tokens=300
        )
        
        analysis_text = analysis_response.choices[0].message.content.strip()
        
        # Parse JSON response
        # Remove markdown code blocks if present
        if '```json' in analysis_text:
            analysis_text = analysis_text.split('```json')[1].split('```')[0].strip()
        elif '```' in analysis_text:
            analysis_text = analysis_text.split('```')[1].split('```')[0].strip()
        
        analysis = json.loads(analysis_text)
        
        print(f"\n{'='*60}")
        print(f"AI ANALYSIS:")
        print(f"{'='*60}")
        print(f"Tenant Intent: {analysis.get('tenant_intent')}")
        print(f"Tenant Offer: ${analysis.get('tenant_offer')}" if analysis.get('tenant_offer') else "Tenant Offer: None")
        print(f"Should Accept: {analysis.get('should_accept')}")
        print(f"Recommended Counter: ${analysis.get('recommended_counter')}" if analysis.get('recommended_counter') else "Recommended Counter: None")
        print(f"Reasoning: {analysis.get('reasoning')}")
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"⚠️  AI analysis failed, falling back to basic logic: {e}")
        # Fallback: simple regex extraction
        analysis = {
            "tenant_offer": None,
            "tenant_intent": "discussing",
            "should_accept": False,
            "recommended_counter": negotiation['current_target_rent'],
            "reasoning": "Fallback logic"
        }
        
        if tenant_message:
            amounts = re.findall(r'\$?(\d+(?:,\d{3})*)', tenant_message)
            if amounts:
                analysis['tenant_offer'] = int(amounts[0].replace(',', ''))
                if analysis['tenant_offer'] >= negotiation['current_target_rent']:
                    analysis['should_accept'] = True
    
    # =================================================================
    # STEP 2: Generate response letter based on AI's decision
    # =================================================================
    
    if analysis.get('should_accept'):
        # Generate acceptance letter
        letter_prompt = f"""
Generate a warm but BRIEF acceptance confirmation (1-2 short paragraphs):
- Confirm agreed rent: ${analysis.get('tenant_offer') or negotiation['current_target_rent']}/month
- Express excitement about continuing tenancy
- Mention lease paperwork will follow

Be BRIEF and friendly. Do NOT include subject lines, greetings, or signatures.
"""
    else:
        # Generate counteroffer letter
        counter_amount = analysis.get('recommended_counter') or negotiation['current_target_rent']
        letter_prompt = f"""
Generate a brief counteroffer response (1-2 short paragraphs maximum):
- Acknowledge their message warmly
- Propose: ${counter_amount}/month
- Keep it friendly and open to discussion
- NO repetition, NO verbose explanations

Be BRIEF and to the point. Do NOT include subject lines, greetings, or signatures.
"""
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages + [{"role": "user", "content": letter_prompt}],
            temperature=0.7,
            max_tokens=250  # Reduced from 500 to keep responses brief
        )
        
        letter_text = response.choices[0].message.content.strip()
        
        # Save assistant response to history
        add_message_to_history(tenant_email, "assistant", letter_text)
        
    except Exception as e:
        print(f"❌ Error generating letter: {e}")
        return None, "error", {"error": str(e)}
    
    # =================================================================
    # STEP 3: Set status and metadata based on AI's decision
    # =================================================================
    
    if analysis.get('should_accept'):
        status = "accepted"
        agreed_rent = analysis.get('tenant_offer') or negotiation['current_target_rent']
        metadata = {
            'agreed_rent': agreed_rent,
            'tenant_offer': analysis.get('tenant_offer'),
            'ai_reasoning': analysis.get('reasoning')
        }
        update_negotiation(tenant_email, status='accepted', current_target_rent=agreed_rent)
        print(f"✅ ACCEPTED: ${agreed_rent}/month")
        
    else:
        status = "countered"
        counter_amount = analysis.get('recommended_counter') or negotiation['current_target_rent']
        metadata = {
            'management_offer': counter_amount,
            'tenant_offer': analysis.get('tenant_offer'),
            'ai_reasoning': analysis.get('reasoning')
        }
        update_negotiation(tenant_email, current_target_rent=counter_amount)
        print(f"🔄 COUNTERED: ${counter_amount}/month")
    
    return letter_text, status, metadata


# -----------------------------
# API Endpoints
# -----------------------------

@app.route("/ai/estimate-rent", methods=["POST"])
def estimate_rent():
    """
    Estimate market rent using multiple methods
    1. Try Zillow scraping (may be blocked)
    2. Use OpenAI to estimate based on location data
    3. Fallback to current rent + 10%
    """
    data = request.json or {}
    
    address = data.get("address", "").strip()
    city = data.get("city", "").strip()
    state = data.get("state", "").strip()
    zipcode = data.get("zipcode", "").strip()
    current_rent = int(data.get("current_rent", 0))
    
    result = estimate_rent_internal(address, city, state, zipcode, current_rent)
    return jsonify(result)


def estimate_rent_internal(address, city, state, zipcode, current_rent):
    """
    Internal function to estimate rent (used by both estimate-rent endpoint and start-negotiation)
    Returns: dict with estimated_rent, source, confidence
    """
    print(f"\n{'='*60}")
    print(f"RENT ESTIMATION REQUEST")
    print(f"{'='*60}")
    print(f"Address: {address}, {city}, {state} {zipcode}")
    print(f"Current Rent: ${current_rent}/month")
    
    # Method 1: Try Zillow scraping
    print("\n🔍 Method 1: Trying Zillow scraping...")
    estimated_rent = get_zillow_rent_estimate(address, city, state, zipcode)
    
    if estimated_rent:
        print(f"✅ Zillow estimate: ${estimated_rent}")
        print(f"{'='*60}\n")
        return {
            "estimated_rent": estimated_rent,
            "source": "zillow",
            "confidence": "high"
        }
    
    # Method 2: Use AI to estimate based on location
    print("🤖 Method 2: Using AI to estimate market rent...")
    try:
        ai_prompt = f"""
Based on current real estate market data, estimate the fair market rent for this property:

Property: {address}, {city}, {state} {zipcode}
Current Rent: ${current_rent}/month

Consider:
- Location (city, state, neighborhood quality)
- Typical rent prices in {city}, {state}
- Current market conditions
- The fact that current rent is ${current_rent}

Respond with ONLY a single number (the estimated monthly rent in dollars).
Do not include dollar signs, commas, or any other text.
Just the number.

Example response: 2850
"""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a real estate market analyst. Provide accurate rent estimates based on location data."},
                {"role": "user", "content": ai_prompt}
            ],
            temperature=0.3,
            max_tokens=50
        )
        
        ai_response = response.choices[0].message.content.strip()
        # Extract just the number
        ai_rent = int(re.sub(r'[^\d]', '', ai_response))
        
        # Sanity check: AI estimate should be reasonable
        if 500 <= ai_rent <= 50000 and abs(ai_rent - current_rent) / current_rent <= 0.5:
            print(f"✅ AI estimate: ${ai_rent}")
            print(f"{'='*60}\n")
            return {
                "estimated_rent": ai_rent,
                "source": "ai_estimate",
                "confidence": "medium"
            }
        else:
            print(f"⚠️  AI estimate ${ai_rent} seems unreasonable, skipping")
            
    except Exception as e:
        print(f"❌ AI estimation failed: {e}")
    
    # Method 3: Fallback to current rent + 10%
    print("📊 Method 3: Using fallback (current rent + 10%)...")
    estimated_rent = int(current_rent * 1.1)
    print(f"✅ Fallback estimate: ${estimated_rent}")
    print(f"{'='*60}\n")
    
    return {
        "estimated_rent": estimated_rent,
        "source": "fallback_estimate",
        "confidence": "low"
    }


@app.route("/ai/start-negotiation", methods=["POST"])
def start_negotiation():
    """
    Start a NEW negotiation and generate initial letter using AI
    Note: target_rent is optional - if not provided, will use estimate-rent
    """
    data = request.json or {}

    tenant_name = data.get("tenant_name", "Tenant")
    tenant_email = data.get("tenant_email")
    address = data.get("address", "")
    city = data.get("city", "")
    state = data.get("state", "")
    zipcode = data.get("zipcode", "")
    current_rent = int(data.get("current_rent", 0))
    
    # Target rent is now OPTIONAL
    # If not provided, we'll estimate it
    target_rent = data.get("target_rent")
    
    if not tenant_email:
        return jsonify({"error": "tenant_email is required"}), 400
    
    if not current_rent:
        return jsonify({"error": "current_rent is required"}), 400
    
    # If no target_rent provided, estimate it
    if not target_rent:
        print(f"⚙️  No target_rent provided, using estimation...")
        
        # Try to estimate rent
        estimated = estimate_rent_internal(address, city, state, zipcode, current_rent)
        target_rent = estimated.get("estimated_rent", int(current_rent * 1.1))
        
        print(f"   Estimated target: ${target_rent} (source: {estimated.get('source', 'fallback')})")
    else:
        target_rent = int(target_rent)
        print(f"   Using provided target: ${target_rent}")

    # Create new negotiation
    create_negotiation(
        tenant_name=tenant_name,
        address=address,
        city=city,
        state=state,
        zipcode=zipcode,
        current_rent=current_rent,
        target_rent=target_rent,
        tenant_email=tenant_email
    )

    # Generate initial letter using AI (no tenant message yet)
    letter_text, _, _ = negotiate_with_ai(tenant_email, tenant_message=None)
    
    if not letter_text:
        return jsonify({"error": "Failed to generate initial letter"}), 500

    return jsonify({
        "status": "initial",
        "letter_text": letter_text,
        "target_rent": target_rent
    })


@app.route("/ai/continue-negotiation", methods=["POST"])
def continue_negotiation():
    """
    Handle tenant replies and continue the negotiation conversation
    """
    data = request.json or {}

    tenant_email = data.get("tenant_email")
    tenant_message = data.get("tenant_message", "")

    print(f"DEBUG: tenant_email = {tenant_email}")
    print(f"DEBUG: tenant_message = {tenant_message}")
    
    if not tenant_email:
        return jsonify({"error": "tenant_email is required"}), 400
    
    if not tenant_message:
        return jsonify({"error": "tenant_message is required"}), 400

    negotiation = get_negotiation(tenant_email)

    if negotiation is None:
        return jsonify({"error": "No active negotiation found for this email"}), 404

    # Let AI handle the entire conversation
    letter_text, status, metadata = negotiate_with_ai(tenant_email, tenant_message)
    
    if letter_text:
        return jsonify({
            "status": status,
            "letter_text": letter_text,
            **metadata
        })
    else:
        return jsonify({"error": metadata.get("error", "Unknown error")}), 500


@app.route("/ai/get-negotiation-context", methods=["POST"])
def get_negotiation_context():
    """
    Get full context of a negotiation by email including conversation history
    """
    data = request.json or {}
    tenant_email = data.get("tenant_email")
    
    if not tenant_email:
        return jsonify({"error": "tenant_email required"}), 400
    
    negotiation = get_negotiation(tenant_email)
    
    if not negotiation:
        return jsonify({"error": "No negotiation found"}), 404
    
    # Get conversation history
    history = json.loads(negotiation['conversation_history'] or '[]')
    
    return jsonify({
        "tenant_name": negotiation["tenant_name"],
        "tenant_email": negotiation["tenant_email"],
        "address": negotiation["address"],
        "city": negotiation["city"],
        "state": negotiation["state"],
        "zipcode": negotiation["zipcode"],
        "current_rent": negotiation["current_rent"],
        "initial_target_rent": negotiation["initial_target_rent"],
        "current_target_rent": negotiation["current_target_rent"],
        "status": negotiation["status"],
        "conversation_history": history,
        "created_at": negotiation["created_at"],
        "updated_at": negotiation["updated_at"]
    })


@app.route("/ai/generate-lease", methods=["POST"])
def generate_lease():
    """
    Generate a filled lease PDF using form fields
    """
    data = request.json or {}
    
    tenant_name = data.get("tenant_name", "")
    landlord_name = data.get("landlord_name", "Vincent Lo")
    address = data.get("address", "")
    city = data.get("city", "")
    state = data.get("state", "")
    zipcode = data.get("zipcode", "")
    agreed_rent = int(data.get("agreed_rent", 0))
    commencement_date = data.get("commencement_date", "")
    lease_term_months = int(data.get("lease_term_months", 12))
    
    security_deposit = agreed_rent * 2
    full_address = f"{address}, {city}, {state} {zipcode}"
    
    # Calculate lease end date
    lease_end_date = ""
    if commencement_date:
        try:
            start = datetime.strptime(commencement_date, "%Y-%m-%d")
            end = start + relativedelta(months=lease_term_months)
            lease_end_date = end.strftime("%Y-%m-%d")
        except:
            pass
    
    template_path = "/Users/vincentlo/rent-bot/lease_template.pdf"
    output_filename = f"lease_{tenant_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
    output_path = f"/Users/vincentlo/rent-bot/{output_filename}"
    
    if not os.path.exists(template_path):
        return jsonify({"error": f"Template not found at: {template_path}"}), 404
    
    try:
        reader = PdfReader(template_path)
        writer = PdfWriter(clone_from=reader)
        
        # Fill form fields on page 1
        field_values = {
            # Landlord and Tenant info
            "Landlord and": landlord_name,
            "Tenant agree as follows": tenant_name,
            
            # Property address
            "Premises": full_address,
            "The Premises are for the sole use as a personal residence by the following named persons only": tenant_name,
            
            # Monthly rent (Section 3A)
            "Tenant agrees to pay": f"${agreed_rent:,}",
            
            # Security deposit (Section 4A and table)
            "Tenant agrees to pay_2": f"${security_deposit:,}",
            "Total DueSecurity Deposit": f"${security_deposit:,}",
            
            # Lease term dates (Section 2)
            "TERM The term begins on date": commencement_date,
            "Lease and shall terminate on date": lease_end_date,
        }
        
        writer.update_page_form_field_values(
            writer.pages[0],
            field_values,
            auto_regenerate=False
        )
        
        # This makes PDF viewers show the filled values properly
        writer.set_need_appearances_writer(True)
        
        # Write filled PDF
        with open(output_path, 'wb') as output_file:
            writer.write(output_file)
        
        print(f"✅ Generated filled lease PDF: {output_path}")
        print(f"   Tenant: {tenant_name}")
        print(f"   Property: {full_address}")
        print(f"   Monthly Rent: ${agreed_rent:,}")
        print(f"   Security Deposit: ${security_deposit:,}")
        print(f"   Lease Term: {commencement_date} to {lease_end_date}")
        
        return jsonify({
            "success": True,
            "pdf_path": output_path,
            "filename": output_filename,
            "download_url": f"http://127.0.0.1:5000/ai/download-lease/{output_filename}",
            "tenant_name": tenant_name,
            "tenant_email": data.get("tenant_email"),
            "landlord_email": data.get("landlord_email", "vincentlo2007@gmail.com"),
            "agreed_rent": agreed_rent,
            "security_deposit": security_deposit,
            "commencement_date": commencement_date,
            "lease_end_date": lease_end_date
        })
        
    except Exception as e:
        print(f"❌ Error generating lease: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/ai/download-lease/<filename>", methods=["GET"])
def download_lease(filename):
    """
    Download a generated lease PDF
    """
    # Security: only allow PDF files and prevent directory traversal
    if not filename.endswith('.pdf'):
        return jsonify({"error": "Invalid file type"}), 400
    
    # Remove any path characters for security
    filename = os.path.basename(filename)
    
    file_path = f"/Users/vincentlo/rent-bot/{filename}"
    
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    
    return send_file(
        file_path,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


# -----------------------------
# Run the app
# -----------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)