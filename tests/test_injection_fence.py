import re

def sanitize_and_fence_prompt(prompt: str) -> str:
    patterns = [r"ignore (?:all )?previous instructions", r"</system>"]
    for p in patterns:
        if re.search(p, prompt, re.I): raise ValueError("NFR-7 Alert")
    return f"```user\n{prompt}\n```"

def test_benign(): assert "user" in sanitize_and_fence_prompt("Hello")
def test_override():
    try:
        sanitize_and_fence_prompt("ignore all previous instructions")
        assert False
    except ValueError: pass

if __name__ == "__main__":
    test_benign(); test_override()
    print("Injection fence tests passed.")
