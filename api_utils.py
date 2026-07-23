# ==========================================================
# api_utils.py — OpenRouter communication and output handling
# ==========================================================
#
# Security: the API key is NEVER hard-coded and is NEVER written to disk.
# It is held only in memory for the current process/session. Set it with
# set_api_key() (the app does this from the in-memory UI field) or via the
# OPENROUTER_API_KEY environment variable.
#
# This module keeps the same public surface the cleanup scripts already use:
#   send_prompt(prompt, persona, step_name, ...) -> str | None
#   extract_between_markers(text, start_marker, end_marker) -> list[str]
# ==========================================================

import requests
import re
import time
import os

# === RUNTIME CONFIGURATION (in memory only) =============================== #
# No real key is ever stored here. Empty string means "not configured yet".
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("RIGOR_MODEL", "deepseek/deepseek-chat")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Saving raw LLM replies to disk is OFF by default to avoid clutter. The app
# can turn it on for debugging via configure(save_outputs=True).
SAVE_OUTPUTS = os.environ.get("RIGOR_SAVE_LLM_OUTPUTS", "").lower() in {"1", "true", "yes"}
OUTPUT_DIR = os.environ.get("RIGOR_LLM_OUTPUT_DIR", "outputs")

# Cap the completion tokens we ask OpenRouter to reserve. Without this, the
# request omits max_tokens and OpenRouter reserves the model's full output
# ceiling (e.g. 65536), which can exceed the account balance and trigger a 402
# pre-flight rejection even though a repair patch is only a few triples.
# Overridable via RIGOR_LLM_MAX_TOKENS.
MAX_TOKENS = int(os.environ.get("RIGOR_LLM_MAX_TOKENS", "8000"))


def set_api_key(key: str) -> None:
    """Set the OpenRouter key in memory for this session only (not persisted)."""
    global API_KEY
    API_KEY = (key or "").strip()


def set_model(model: str) -> None:
    global MODEL
    if model:
        MODEL = model


def configure(api_key: str = None, model: str = None, save_outputs: bool = None) -> None:
    """Convenience: configure key/model/debug-saving in one call (in memory)."""
    global SAVE_OUTPUTS
    if api_key is not None:
        set_api_key(api_key)
    if model is not None:
        set_model(model)
    if save_outputs is not None:
        SAVE_OUTPUTS = bool(save_outputs)


def extract_between_markers(text: str, start_marker: str, end_marker: str):
    """Extract all blocks between given markers."""
    if not text:
        return []
    pattern = re.compile(re.escape(start_marker) + r"(.*?)" + re.escape(end_marker), re.DOTALL)
    matches = pattern.findall(text)
    return [m.strip() for m in matches if m.strip()]


def save_output_to_file(content: str, filename: str):
    """Save extracted output to a text file (debug only; never the API key)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content.strip() + "\n")
    return filepath


def send_prompt(prompt: str, persona: str, step_name: str, max_retries: int = 3, wait_time: int = 20, max_tokens: int = None):
    """
    Send a single stateless prompt to OpenRouter, extract content between the
    output markers, and return it. Returns None on HTTP error / exhausted
    rate-limit retries (preserving the original contract the cleanup modules
    expect). Raises RuntimeError only when no API key has been configured.
    """
    if not API_KEY:
        raise RuntimeError(
            "OpenRouter API key is not set. Configure it in the app settings "
            "(it is kept in memory for this session only)."
        )

    system_prompt = {
        "role": "system",
        "content": f"{persona}\n"
                   "Respond ONLY between ###start_output### and ###end_output### markers.\n"
                   "For Turtle syntax responses, use ONLY ###start_turtle### and ###end_turtle### markers.\n"
                   "Avoid explanations or text outside these markers."
    }
    user_prompt = {"role": "user", "content": prompt}

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": MODEL, "messages": [system_prompt, user_prompt],
                      "max_tokens": max_tokens if max_tokens is not None else MAX_TOKENS},
                timeout=180,
            )
        except requests.RequestException as e:
            print(f"❌ Network error contacting OpenRouter ({step_name}): {e}")
            return None

        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            print(f"\n--- MODEL RESPONSE ({step_name}) ---\n{reply[:300]}...\n")
            extracted = extract_between_markers(reply, "###start_output###", "###end_output###")
            if not extracted:
                extracted = [reply]
            joined = "\n\n".join(extracted)
            if SAVE_OUTPUTS:
                try:
                    save_output_to_file(joined, f"{step_name}.txt")
                except Exception as e:
                    print(f"⚠️ Could not save debug output: {e}")
            return joined

        elif response.status_code == 429:
            if attempt < max_retries:
                print(f"⏳ Rate limit hit (429). Retrying in {wait_time}s... (Attempt {attempt}/{max_retries})")
                time.sleep(wait_time)
                continue
            print("❌ Max retry attempts reached due to rate limiting.")
            return None

        else:
            print(f"❌ Error {response.status_code}: {response.text[:500]}")
            return None



def send_prompt_raw(prompt: str, persona: str, step_name: str, max_retries: int = 3, wait_time: int = 20, max_tokens: int = None) -> str:
    """Send a prompt and return the model response verbatim.

    Use this for structured multi-block protocols (for example decision + add +
    delete markers). Unlike ``send_prompt``, it does not extract or rewrite any
    marker block.
    """
    if not API_KEY:
        raise RuntimeError(
            "OpenRouter API key is not set. Configure it in the app settings "
            "(it is kept in memory for this session only)."
        )

    system_prompt = {
        "role": "system",
        "content": (
            f"{persona}\n"
            "Follow the exact output markers requested in the user prompt. "
            "Do not add prose outside those markers."
        ),
    }
    user_prompt = {"role": "user", "content": prompt}

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": [system_prompt, user_prompt],
                    "max_tokens": max_tokens if max_tokens is not None else MAX_TOKENS,
                },
                timeout=180,
            )
        except requests.RequestException as exc:
            if attempt < max_retries:
                time.sleep(wait_time)
                continue
            raise RuntimeError(f"Network error contacting OpenRouter ({step_name}): {exc}") from exc

        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            print(f"\n--- MODEL RESPONSE ({step_name}) ---\n{reply[:500]}...\n")
            if SAVE_OUTPUTS:
                try:
                    save_output_to_file(reply, f"{step_name}.txt")
                except Exception as exc:
                    print(f"⚠️ Could not save debug output: {exc}")
            return reply
        if response.status_code == 429 and attempt < max_retries:
            time.sleep(wait_time)
            continue
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:500]}")

    raise RuntimeError(f"OpenRouter request failed for {step_name}")

def load_ontology_as_context(ontology_file: str) -> str:
    """Load the current full ontology .ttl file as text for LLM context."""
    try:
        with open(ontology_file, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
