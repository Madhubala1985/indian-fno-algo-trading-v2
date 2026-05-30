import json

from kiteconnect import KiteConnect


CONFIG_PATH = "configs/kite_config.json"


with open(CONFIG_PATH, "r") as f:
    config = json.load(f)


api_key = config["api_key"]

api_secret = config["api_secret"]


kite = KiteConnect(
    api_key=api_key
)


print("\nOpen this URL in browser:\n")

print(kite.login_url())

print(
    "\nAfter login copy the "
    "`request_token` from URL.\n"
)


request_token = input(
    "Paste request_token: "
).strip()


session_data = kite.generate_session(
    request_token=request_token,
    api_secret=api_secret,
)


access_token = (
    session_data["access_token"]
)


config["access_token"] = (
    access_token
)


with open(CONFIG_PATH, "w") as f:

    json.dump(
        config,
        f,
        indent=4,
    )


print(
    "\nAccess token generated "
    "and saved.\n"
)
