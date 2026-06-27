import re

with open("logs/amazon_shift_alert.log") as f:
    lines = f.readlines()

print("Searching for lines with dates...")
count = 0
for i, line in enumerate(lines):
    if re.search(r"\d{4}-\d{2}-\d{2}", line) or re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", line):
        print(f"Line {i}: {line.strip()}")
        count += 1
        if count >= 10:
            break
