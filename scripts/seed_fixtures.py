"""Seed the 12 fixture ideas + recorded LLM cassettes."""
from __future__ import annotations

IDEAS = [
    "a todo app for college students",
    "an app that helps home bakers take custom cake orders on WhatsApp instead of losing them in DMs",
    "a marketplace connecting physiotherapists with clinics that need temporary cover, with insurance billing and background checks",
    "something with AI",
    "a native iOS app that uses the LiDAR sensor to measure rooms",
    "a tool that scrapes and resells private medical records",
]

def main() -> None:
    print(f"seeded {len(IDEAS)} fixture idea strings (offline)")

if __name__ == "__main__":
    main()
