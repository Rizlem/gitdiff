import os
import json
import re
import asyncio
from datetime import datetime
import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import AsyncGroq
from config_loader import load_config

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

IDLE_FLUSH_SECONDS = 15
MAX_DIFF_CHARS = 24000  # keep prompts within Groq limits

# Load YAML Config
config = load_config()
TARGET_CHANNEL_ID = config.get("discord", {}).get("target_channel_id")

DISCORD_USER_MAPPINGS = {}
for key, user_data in config.get("users", {}).items():
    shortcode = user_data["shortcode"]
    discord_username = user_data["discord_username"]
    DISCORD_USER_MAPPINGS[shortcode] = discord_username

END_SIGNAL = "<<<GMD_END_OF_DIFF_7f3a>>>"

SYSTEM_PROMPT = (
    "You are an expert developer assistant. Analyze the provided git diff (which includes metadata at the top) and output a valid JSON object. "
    "The JSON object must contain exactly these two keys:\n\n"
    "\"commit\": The commit message formatted with literal '\\n' characters for line breaks. You MUST adhere strictly to these rules:\n"
    "1. Find 'Shortcode: ' in the metadata and extract the exact value. Start your message with this value wrapped in brackets (e.g., [mb1425]). Do NOT output empty brackets [ ].\n"
    "2. Write the subject and ALL bullet points in the imperative, present-tense command form (e.g., 'Add error handling' NOT 'Added error handling').\n"
    "3. ABSOLUTELY NO full stops (periods) at the end of the subject line or any of the bullet points.\n"
    "4. Use at most 5 bullet points, one per distinct change, most important first.\n"
    "5. Use the name of the most-changed file, without its extension, as the (scope).\n"
    "6. Choose <type> with these rules, in priority order:\n"
    "   - feat: the diff adds any new capability, function, option or behavior\n"
    "   - fix: the diff mainly corrects incorrect behavior and adds nothing new\n"
    "   - refactor: code is restructured with no change in behavior\n"
    "   - docs: only documentation or comments changed\n"
    "   - chore: only config, dependencies, build or tooling changed\n"
    "   If the diff mixes several types, pick the FIRST matching type in that order. Never use chore for changes to source logic.\n"
    "7. Do NOT add any paragraph, explanation or text after the bullet points. The commit message must end with the last bullet.\n\n"
    "Format EXACTLY like this:\n"
    "[extracted_shortcode] type(scope): imperative command subject\\n\\n"
    "- imperative description of change 1 without a full stop\\n"
    "- imperative description of change 2 without a full stop\n\n"
    "\"summary\": A natural, conversational explanation of the changes. Make it sound like a human wrote it. Escape internal quotes."
)

# Initialize Discord Intents & Bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize Groq Async Client
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

flush_tasks = {}  # author_id -> pending idle-flush task

async def idle_flush(message, author_id):
    try:
        await asyncio.sleep(IDLE_FLUSH_SECONDS)
    except asyncio.CancelledError:
        return
    flush_tasks.pop(author_id, None)
    print(f"[idle-flush] no end signal after {IDLE_FLUSH_SECONDS}s, processing buffer")
    await finalize_diff(message, author_id)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Monitoring and formatting channel ID: {TARGET_CHANNEL_ID}")
    print("------")

def safe_field(text, limit=1000):
    """Coerce to str and trim so Discord never rejects the embed field."""
    text = "" if text is None else str(text)
    if not text.strip():
        text = "N/A"
    return text if len(text) <= limit else text[:limit - 3] + "..."

async def safe_delete(message):
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

def strip_fences(text):
    """Remove the ```diff wrapper the CLI adds to each chunk."""
    text = re.sub(r'^```diff\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    return text

def capitalize_commit(msg):
    """Uppercase the first letter after the subject colon and after each bullet."""
    # Subject line: "[code] type(scope): subject" -> capitalize the first letter after ": "
    msg = re.sub(
        r'^(\s*\[[^\]]*\]\s*\w+(?:\([^)]*\))?:\s*)([a-z])',
        lambda m: m.group(1) + m.group(2).upper(),
        msg,
        count=1,
    )
    # Bullets: "- text" or "* text" -> capitalize the first letter of the text
    msg = re.sub(
        r'^(\s*[-*]\s+)([a-z])',
        lambda m: m.group(1) + m.group(2).upper(),
        msg,
        flags=re.MULTILINE,
    )
    return msg

def diff_stats(diff_text):
    """Return ('+A/-R', [changed files]) computed directly from the diff."""
    added = removed = 0
    files = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1].strip())
        elif line.startswith(("--- a/", "--- /dev/null", "+++ b/", "+++ /dev/null")):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"+{added}/-{removed}", files

def trim_to_bullets(msg):
    """Drop anything after the last bullet point."""
    lines = msg.splitlines()
    last_bullet = -1
    for i, line in enumerate(lines):
        if re.match(r'^\s*[-*]\s+', line):
            last_bullet = i
    if last_bullet == -1:
        return msg  # no bullets found, leave it alone
    return "\n".join(lines[:last_bullet + 1]).rstrip()

# Dictionary to buffer multi-part diffs from the webhook
diff_buffers = {}

async def finalize_diff(message, author_id):
    # Take the buffer and clear it in one step
    full_diff_text = diff_buffers.pop(author_id, "").strip()

    if not full_diff_text:
        print("[finalize] buffer empty, nothing to do")
        return

    try:
        # Determine user mention
        mapped_mention = message.author.mention
        for shortcode, discord_username in DISCORD_USER_MAPPINGS.items():
            if shortcode in full_diff_text:
                if message.guild:
                    found_member = discord.utils.find(
                        lambda m: m.name.lower() == discord_username.lower() or
                                  (m.global_name and m.global_name.lower() == discord_username.lower()),
                        message.guild.members
                    )
                    mapped_mention = found_member.mention if found_member else f"@{discord_username}"
                break

        stats, files = diff_stats(full_diff_text)
        diff_for_llm = full_diff_text
        if len(diff_for_llm) > MAX_DIFF_CHARS:
            diff_for_llm = diff_for_llm[:MAX_DIFF_CHARS] + "\n[DIFF TRUNCATED]"

        print(f"--- Processing new diff ---\nExtracted {len(full_diff_text)} characters.")

        async with message.channel.typing():
            chat_completion = await groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"Files changed: {', '.join(files) or 'unknown'}\n"
                        f"Line counts: {stats}\n\n"
                        f"Here is the code change:\n\n{diff_for_llm}\n\nPlease provide the JSON response:"
                    )},
                ],
                temperature=0.2,
                seed=42,
                max_tokens=8192,
                reasoning_effort="low",
            )

        choice = chat_completion.choices[0]
        raw_response = (choice.message.content or "").strip()

        if not raw_response:
            raise RuntimeError(
                f"Groq returned an empty response (finish_reason={choice.finish_reason}). "
                "The diff may be too large or the token budget was used up."
            )

        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        response_text = json_match.group(0) if json_match else raw_response

        lines_added_removed, commit_msg, summary_text = "+0/-0", "No commit message generated.", "No summary provided."

        try:
            data = json.loads(response_text)
            commit_msg = data.get("commit", commit_msg)
            summary_text = data.get("summary", summary_text)
        except json.JSONDecodeError:
            print("Standard JSON parsing failed. Falling back to regex extraction.")
            m = re.search(r'"lines"\s*:\s*"(.*?)"', response_text, re.IGNORECASE)
            if m: lines_added_removed = m.group(1)
            m2 = re.search(r'"commit"\s*:\s*"(.*?)"\s*,\s*"summary"', response_text, re.IGNORECASE | re.DOTALL)
            if m2: commit_msg = m2.group(1)
            m3 = re.search(r'"summary"\s*:\s*"(.*?)"\s*\}?\s*$', response_text, re.IGNORECASE | re.DOTALL)
            if m3: summary_text = m3.group(1)
            if not (m or m2 or m3):
                summary_text = f"**Raw AI Output (failed to parse):**\n{raw_response[:900]}"

        lines_added_removed = stats
        commit_msg = str(commit_msg).replace('\\n', '\n')
        commit_msg = capitalize_commit(commit_msg)
        commit_msg = trim_to_bullets(commit_msg)

        embed = discord.Embed(color=discord.Color.blue())
        embed.add_field(name="Lines added/removed", value=safe_field(lines_added_removed), inline=False)
        embed.add_field(name="Commit message:", value=f"```\n{safe_field(commit_msg, 1000)}\n```", inline=False)
        embed.add_field(name="Summary of changes by User", value=safe_field(summary_text), inline=False)

        await message.channel.send(content=f"**User:** {mapped_mention}", embed=embed)
        await message.channel.send(content="-" * 84)

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            await message.channel.send(
                f"⚠️ **Diff analysis failed** ({type(e).__name__}): `{str(e)[:300]}`"
            )
        except Exception as send_err:
            print(f"Could not post error to Discord: {send_err}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Only handle webhook traffic in the target channel; let everything else through
    if message.channel.id != TARGET_CHANNEL_ID or message.webhook_id is None:
        await bot.process_commands(message)
        return

    # --- Extract text ---
    extracted_text = message.content or ""
    for embed in message.embeds:
        if embed.title:
            extracted_text += f"\n{embed.title}"
        if embed.description:
            extracted_text += f"\n{embed.description}"
        for field in embed.fields:
            extracted_text += f"\n{field.name}\n{field.value}"
    extracted_text = extracted_text.strip()
    if not extracted_text:
        return

    author_id = message.author.id
    is_end = (extracted_text == END_SIGNAL)  # EXACT match, not substring

    print(f"[recv] on webhook_id len={len(extracted_text)} is_end={is_end} buffered={len(diff_buffers.get(author_id, ''))}")

    # --- Buffer BEFORE any await so chunk order is preserved ---
    if not is_end:
        diff_buffers[author_id] = diff_buffers.get(author_id, "") + "\n" + strip_fences(extracted_text)

    # --- Delete in the background so it can't block or reorder anything ---
    asyncio.create_task(safe_delete(message))

    # Any new message for this author cancels a pending idle flush
    pending = flush_tasks.pop(author_id, None)
    if pending:
        pending.cancel()

    if not is_end:
        # Chunk: (re)start the idle timer, then wait for more
        flush_tasks[author_id] = asyncio.create_task(idle_flush(message, author_id))
        return

    await finalize_diff(message, author_id)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        print("Error: Tokens are missing from environment variables!")
    else:
        bot.run(DISCORD_TOKEN)