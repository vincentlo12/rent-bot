"""
Test script to demonstrate the conversational negotiation flow
"""

import requests
import json
import time

BASE_URL = "http://127.0.0.1:5000"

def print_response(title, response):
    """Pretty print API responses"""
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")
    print(f"Status Code: {response.status_code}")
    
    try:
        data = response.json()
        print(json.dumps(data, indent=2))
    except:
        print(response.text)
    
    print(f"{'='*60}\n")
    return response.json() if response.status_code == 200 else None


def test_full_negotiation():
    """Test a complete negotiation flow"""
    
    tenant_email = f"test_{int(time.time())}@example.com"
    
    print("\n🏠 TESTING CONVERSATIONAL RENT NEGOTIATION")
    print(f"Tenant Email: {tenant_email}\n")
    
    # Step 1: Start negotiation
    print("Step 1: Starting negotiation...")
    response = requests.post(f"{BASE_URL}/ai/start-negotiation", json={
        "tenant_name": "Alice Johnson",
        "tenant_email": tenant_email,
        "address": "456 Oak Avenue",
        "city": "San Francisco",
        "state": "CA",
        "zipcode": "94102",
        "current_rent": 2500,
        "target_rent": 2800
    })
    
    result = print_response("📧 INITIAL LETTER", response)
    if not result:
        print("❌ Failed to start negotiation")
        return
    
    print(f"\n📝 Management says:\n{result['letter_text']}\n")
    input("Press Enter to continue...")
    
    # Step 2: Tenant counteroffers
    print("\nStep 2: Tenant sends counteroffer...")
    response = requests.post(f"{BASE_URL}/ai/continue-negotiation", json={
        "tenant_email": tenant_email,
        "tenant_message": "Hi, thank you for letting me know. I've really enjoyed living here, but $2800 is quite a jump. Would you be willing to consider $2600/month? That would work much better with my budget."
    })
    
    result = print_response("📧 COUNTEROFFER RESPONSE", response)
    if not result:
        print("❌ Failed to continue negotiation")
        return
    
    print(f"\n📝 Management says:\n{result['letter_text']}\n")
    input("Press Enter to continue...")
    
    # Step 3: Tenant accepts or negotiates further
    print("\nStep 3: Tenant responds again...")
    response = requests.post(f"{BASE_URL}/ai/continue-negotiation", json={
        "tenant_email": tenant_email,
        "tenant_message": "I appreciate you working with me. How about we meet in the middle at $2700? I think that's fair for both of us."
    })
    
    result = print_response("📧 FINAL RESPONSE", response)
    if not result:
        print("❌ Failed to continue negotiation")
        return
    
    print(f"\n📝 Management says:\n{result['letter_text']}\n")
    
    if result.get('status') == 'accepted':
        print(f"✅ DEAL AGREED at ${result.get('agreed_rent', 'N/A')}/month!")
    else:
        print("🔄 Negotiation continues...")
    
    # Step 4: View conversation history
    print("\nStep 4: Viewing conversation history...")
    response = requests.post(f"{BASE_URL}/ai/get-negotiation-context", json={
        "tenant_email": tenant_email
    })
    
    result = print_response("📊 FULL NEGOTIATION CONTEXT", response)
    
    if result:
        print("\n📜 CONVERSATION TIMELINE:")
        for i, msg in enumerate(result['conversation_history'], 1):
            speaker = "🏢 Management" if msg['role'] == 'assistant' else "👤 Tenant"
            print(f"\n{i}. {speaker} ({msg['timestamp']}):")
            print(f"   {msg['content'][:100]}...")


def test_immediate_acceptance():
    """Test when tenant immediately accepts the initial offer"""
    
    tenant_email = f"test_accept_{int(time.time())}@example.com"
    
    print("\n\n🎯 TESTING IMMEDIATE ACCEPTANCE")
    print(f"Tenant Email: {tenant_email}\n")
    
    # Start negotiation
    response = requests.post(f"{BASE_URL}/ai/start-negotiation", json={
        "tenant_name": "Bob Smith",
        "tenant_email": tenant_email,
        "address": "789 Pine Street",
        "city": "Oakland",
        "state": "CA",
        "zipcode": "94601",
        "current_rent": 2000,
        "target_rent": 2200
    })
    
    result = print_response("📧 INITIAL LETTER", response)
    print(f"\n📝 Management proposes: $2200/month\n")
    
    # Tenant accepts immediately
    response = requests.post(f"{BASE_URL}/ai/continue-negotiation", json={
        "tenant_email": tenant_email,
        "tenant_message": "That sounds fair! I'm happy to accept $2200/month. When should I expect the new lease agreement?"
    })
    
    result = print_response("📧 ACCEPTANCE RESPONSE", response)
    
    if result and result.get('status') == 'accepted':
        print(f"✅ IMMEDIATE ACCEPTANCE at ${result.get('agreed_rent')}/month!")
    else:
        print("⚠️ Expected immediate acceptance but didn't get it")


def test_estimate_rent():
    """Test the rent estimation endpoint"""
    
    print("\n\n💰 TESTING RENT ESTIMATION")
    
    response = requests.post(f"{BASE_URL}/ai/estimate-rent", json={
        "address": "123 Main Street",
        "city": "San Francisco",
        "state": "CA",
        "zipcode": "94102",
        "current_rent": 2500
    })
    
    result = print_response("💵 RENT ESTIMATE", response)
    
    if result:
        print(f"Estimated Rent: ${result.get('estimated_rent')}/month")
        print(f"Source: {result.get('source')}")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("RENT-BOT CONVERSATIONAL API TEST SUITE")
    print("="*60)
    
    try:
        # Run tests
        test_full_negotiation()
        test_immediate_acceptance()
        test_estimate_rent()
        
        print("\n\n" + "="*60)
        print("✅ ALL TESTS COMPLETED")
        print("="*60)
        
    except requests.exceptions.ConnectionError:
        print("\n❌ ERROR: Could not connect to Flask server")
        print("Make sure the server is running: python app.py")
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()