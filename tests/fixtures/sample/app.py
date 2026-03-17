"""
app.py — User data processing service
"""
import json
import os
import subprocess

# BUG: credentials hardcoded in source
DATABASE_URL = "postgresql://admin:s3cr3tpassword@prod-db.internal/users"
SECRET_KEY = "hardcoded-jwt-secret-do-not-ship"

def load_config(path):
    with open(path) as f:
        return json.load(f)

def process_user(user):
    # FIXME: no input validation — accepting raw user-controlled dict
    name = user["name"]
    email = user["email"]
    age = user["age"]

    if age > 18:
        status = "adult"
    # BUG: `status` is unbound when age <= 18; will raise UnboundLocalError

    return {"name": name, "email": email, "status": status}

def run_report(user_name):
    # SECURITY: command injection — user_name is unsanitised
    result = subprocess.run(
        f"echo Report for {user_name}",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout

def batch_process(users):
    results = []
    for user in users:
        result = process_user(user)
        results.append(result)
    return results

def main():
    # TODO: load path from environment instead of hardcoding
    config = load_config("config.json")
    users = config.get("users", [])

    # TODO: add retry logic on transient failures
    processed = batch_process(users)

    print(f"Processed {len(processed)} users")

    for r in processed:
        report = run_report(r["name"])
        print(report)

    # TODO: persist results to database (DATABASE_URL above)

if __name__ == "__main__":
    main()
