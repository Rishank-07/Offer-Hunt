"""Tavily Search API Smoke Test for OFFERHUNT.

Checks API key presence, search functionality, and prints safe metadata.
NEVER prints or logs API keys.
"""

import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def main():
    api_key = os.getenv("TAVILY_API_KEY")

    print("=== OFFERHUNT Tavily Smoke Test ===")

    if not api_key or api_key == "your_tavily_api_key_here":
        print("\n[FAIL] Error Category: MISSING KEY")
        print("Detail: TAVILY_API_KEY is not set or contains placeholder value in .env file.")
        print("Action required: Please add your valid TAVILY_API_KEY to the .env file.")
        return 1

    try:
        from tavily import TavilyClient
    except ImportError as e:
        print("\n[FAIL] Error Category: SDK MISMATCH")
        print(f"Detail: tavily-python package import failed: {e}")
        return 1

    try:
        client = TavilyClient(api_key=api_key)
        print("Calling Tavily Search API with test query...")
        response = client.search(
            query="OFFERHUNT discount deals retail shopping",
            max_results=2,
        )

        results = response.get("results", [])
        print(f"\n[PASS] Search returned successfully!")
        print(f"Results Count: {len(results)}")
        print("\nSearch Results Metadata (Titles & URLs only):")
        for idx, res in enumerate(results, 1):
            title = res.get("title", "No Title")
            url = res.get("url", "No URL")
            print(f"  {idx}. Title: {title}")
            print(f"     URL:   {url}")

        print("\nNotice: Please check your Tavily dashboard at https://app.tavily.com/ for usage and remaining credits.")
        return 0

    except Exception as exc:
        err_str = str(exc)
        print("\n[FAIL] Tavily Search execution failed.")
        category = "UNKNOWN"
        if "401" in err_str or "unauthorized" in err_str.lower() or "invalid api key" in err_str.lower():
            category = "PERMISSION / INVALID KEY"
        elif "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower():
            category = "QUOTA / RATE LIMIT EXCEEDED"
        elif "timeout" in err_str.lower() or "network" in err_str.lower():
            category = "NETWORK / TIMEOUT"
        else:
            category = f"API ERROR: {exc.__class__.__name__}"

        print(f"Error Category: {category}")
        print(f"Error Summary: {err_str[:250]}")
        print("Notice: Please check your Tavily dashboard at https://app.tavily.com/ for API key status and credits.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
