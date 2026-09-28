import requests
import subprocess
import time
import sys
from config_loader import load_config

# Load configuration and populate variables
config = load_config()

WEBHOOK_URL = config.get("discord", {}).get("webhook_url", "")
MAX_LENGTH = config.get("cli", {}).get("max_chunk_length", 1980)
DELAY_SECONDS = config.get("cli", {}).get("delay_seconds", 1)
END_SIGNAL = "<<<GMD_END_OF_DIFF_7f3a>>>"

USER_MAPPINGS = {}
DISCORD_USER_MAPPINGS = {}

# Build the mapping dictionaries from the YAML users block
for key, user_data in config.get("users", {}).items():
    shortcode = user_data["shortcode"]
    discord_username = user_data["discord_username"]
    
    DISCORD_USER_MAPPINGS[shortcode] = discord_username
    
    for email in user_data.get("git_emails", []):
        USER_MAPPINGS[email] = shortcode

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

# Retrieves the user's git email to be used as a credential for mapping.
def get_git_user_email():
    """Executes git config user.email to identify the user from their local environment."""
    try:
        process = subprocess.run(
            ["git", "config", "user.email"], 
            capture_output=True, 
            text=True
        )
        if process.returncode == 0:
            return process.stdout.strip()
        return "Unknown Email"
    except FileNotFoundError:
        return "Unknown Email"

# Removes empty lines and empty addition/deletion lines from the diff output.
def clean_diff_output(diff_text):
    """Processes the diff text to remove empty lines and lines containing only a plus or minus sign."""
    cleaned_lines = []
    for line in diff_text.splitlines():
        stripped = line.strip()
        if stripped not in ("", "+", "-"):
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)

# Retrieves the output of the git diff command for the current directory.
def get_git_diff():
    """Executes the git diff subprocess and returns the output string, returning None if it is not a git repository."""
    try:
        process = subprocess.run(
            ["git", "diff"], 
            capture_output=True, 
            text=True
        )
        if process.returncode != 0:
            return None
        return process.stdout
    except FileNotFoundError:
        return None

def send_to_discord(content, retries=5):
    """POST to the webhook, retrying on rate limits. Returns True on success."""
    payload = {"content": content}
    for _ in range(retries):
        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        except requests.RequestException as e:
            print(f"Webhook request error: {e}")
            time.sleep(1)
            continue

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 1))
            except Exception:
                wait = 1
            time.sleep(wait + 0.1)
            continue

        print(f"Webhook failed: {response.status_code} {response.text[:200]}")
        return False
    return False

# Chunks and formats the diff string, adds metadata, and sends to Discord.
def process_and_send_diff(result):
    """Prepares metadata, cleans the diff, splits it into chunks, and sends each part to Discord."""
    if result is None:
        return send_to_discord("Not a git directory")
        
    if result.strip() == "":
        return send_to_discord("No uncommitted changes in the current git directory.")

    user_email = get_git_user_email()
    shortcode = USER_MAPPINGS.get(user_email, "UNKNOWN_USER")
    discord_user = DISCORD_USER_MAPPINGS.get(shortcode, "UNKNOWN_USER")
    
    cleaned_result = clean_diff_output(result)

    # Split any single line that is too long to fit in one message
    piece_size = MAX_LENGTH - 50
    lines = []
    for line in cleaned_result.splitlines():
        while len(line) > piece_size:
            lines.append(line[:piece_size])
            line = line[piece_size:]
        lines.append(line)

    current_chunk = f"Email: {user_email} | Shortcode: {shortcode} | Discord User: {discord_user}\n"
    all_successful = True

    for line in lines:
        if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
            if not send_to_discord(f"```diff\n{current_chunk}\n```"):
                all_successful = False
            current_chunk = line + "\n"
            time.sleep(DELAY_SECONDS)
        else:
            current_chunk += line + "\n"

    if current_chunk.strip():
        if not send_to_discord(f"```diff\n{current_chunk}\n```"):
            all_successful = False
        time.sleep(DELAY_SECONDS)  # pause before the end signal to avoid a 429

    # Tell the bot we are done, and count a failure here as a real failure
    if not send_to_discord(END_SIGNAL):
        all_successful = False

    return all_successful

# Orchestrates the script execution and handles cosmetic terminal outputs.
def main():
    """Serves as the main entry point to fetch the diff, print cosmetic text, and output colorized results."""
    print("\nparsing git diff to discord...")
    
    result = get_git_diff()
    success = process_and_send_diff(result)
    
    if success:
        print(f"{COLOR_GREEN}Successfully generated commit message on discord{COLOR_RESET}\n")
    else:
        print(f"{COLOR_RED}Failed to send message to discord{COLOR_RESET}\n")

if __name__ == "__main__":
    main()