"""Gemini API Smoke Test for OFFERHUNT.

Checks API key presence, model connectivity, and safe metadata.
NEVER prints or logs API keys.
"""

import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def main():
    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    print("=== OFFERHUNT Gemini Smoke Test ===")
    print(f"Target Model: {model_name}")

    if not api_key or api_key == "your_gemini_api_key_here":
        print("\n[FAIL] Error Category: MISSING KEY")
        print("Detail: GEMINI_API_KEY is not set or contains placeholder value in .env file.")
        print("Action required: Please add your valid GEMINI_API_KEY to the .env file.")
        return 1

    try:
        from google import genai
    except ImportError as e:
        print("\n[FAIL] Error Category: SDK MISMATCH")
        print(f"Detail: google-genai package import failed: {e}")
        return 1

    try:
        client = genai.Client(api_key=api_key)
        print("Calling Gemini API...")
        response = client.models.generate_content(
            model=model_name,
            contents="Respond with 'OFFERHUNT_SMOKE_OK'",
        )
        response_text = response.text.strip() if response.text else ""
        print(f"\n[PASS] Model responded successfully!")
        print(f"Model: {model_name}")
        print(f"Response Preview: {response_text[:60]}")
        return 0

    except Exception as exc:
        err_str = str(exc)
        print(f"\n[FAIL] Execution failed for model: {model_name}")

        # Classify error category
        category = "UNKNOWN"
        if "403" in err_str or "PERMISSION_DENIED" in err_str or "API_KEY_INVALID" in err_str:
            category = "PERMISSION / INVALID KEY"
        elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            category = "QUOTA EXCEEDED"
        elif "404" in err_str or "NOT_FOUND" in err_str or "models/" in err_str:
            category = "MODEL NOT FOUND / ACCESS RESTRICTED"
        elif "timeout" in err_str.lower() or "connection" in err_str.lower() or "network" in err_str.lower():
            category = "NETWORK / TIMEOUT"
        else:
            category = f"SDK / API ERROR: {exc.__class__.__name__}"

        print(f"Error Category: {category}")
        print(f"Error Summary: {err_str[:250]}")

        # Query available models via SDK to propose alternatives
        print("\nQuerying available models accessible to your key via SDK...")
        try:
            available_models = []
            for m in client.models.list():
                available_models.append(m.name)
            
            supported = [m for m in available_models if "generateContent" in getattr(m, "supported_actions", ["generateContent"]) or "gemini" in m.lower()]
            print(f"Available models ({len(supported)} found):")
            for m in supported[:10]:
                print(f"  - {m}")
            if supported:
                proposed = supported[0]
                print(f"\nProposed alternative model: {proposed}")
                print("NOTE: Do NOT silently switch models; waiting for user approval before changing GEMINI_MODEL.")
        except Exception as list_err:
            print(f"Could not retrieve model list: {list_err}")

        return 1

if __name__ == "__main__":
    sys.exit(main())
