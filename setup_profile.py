"""
Run this script ONCE per profile to log in to Adobe Podcast.
After login, close the browser - the session will be saved.
"""

import sys
sys.path.insert(0, r"A:\Automation Workflows\AudioEnhancementSuite")

from seleniumbase import SB

# ── CHANGE THESE ──────────────────────────────────────────────────────────────
USER_DATA_DIR = r"C:\ChromeProfiles\AdobeProfile1"   # folder where profile is saved
PROFILE_DIR   = "Default"                             # keep as Default
# ─────────────────────────────────────────────────────────────────────────────

print(f"Opening Chrome with profile: {USER_DATA_DIR}")
print("1. Log in to Adobe Podcast")
print("2. Make sure you reach: https://podcast.adobe.com/en/enhance")
print("3. Close the browser window when done")
print()

with SB(
    browser="chrome",
    user_data_dir=USER_DATA_DIR,
    profile=PROFILE_DIR,
    headless=False,
) as sb:
    sb.open("https://podcast.adobe.com/en/enhance")
    print("Browser is open. Log in now...")
    input("Press ENTER here after you have logged in and the Enhance page is visible...")

print()
print("Profile saved! Now add these details in the app:")
print(f"  User Data Dir : {USER_DATA_DIR}")
print(f"  Profile Folder: {PROFILE_DIR}")
