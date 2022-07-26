import base64
import datetime
import hashlib
import json
import random
import re
import requests
import secrets
import string
import sys
import uuid
import webbrowser

nintendo_client_id = "71b963c1b7b6d119" # Hardcoded in app, this is for the NSO app (parental control app has a different ID)
redirect_uri_regex = re.compile(r"npf71b963c1b7b6d119:\/\/auth#session_state=([0-9a-f]{64})&session_token_code=([A-Za-z0-9-._]+)&state=([A-Za-z]{50})")

def parse_redirect_uri(uri):
    m = redirect_uri_regex.match(uri)
    if not m:
        return None

    return (m.group(1), m.group(2), m.group(3))

def generate_challenge():
    # PKCE challenge/response
    # Verifier: 32 random bytes, Base64-encoded
    # Challenge: Those bytes, in hex, hashed with SHA256, Base64-encoded

    verifier = secrets.token_bytes(32)
    verifier_b64 = base64.urlsafe_b64encode(verifier).decode().replace("=", "")

    s256 = hashlib.sha256()
    s256.update(verifier_b64.encode())

    challenge_b64 = base64.urlsafe_b64encode(s256.digest()).decode().replace("=", "")
    return verifier_b64, challenge_b64

def generate_state():
    # OAuth state is just a random opaque string
    alphabet = string.ascii_letters
    return "".join(random.choice(alphabet) for _ in range(50))

rsess = requests.Session()

def call_flapg(id_token, timestamp, request_id, hash, type):
    # Calls the flapg API to get an "f-code" for a login request
    # this is generated by the NSO app but hasn't been reverse-engineered at the moment.
    flapg_resp = rsess.post("https://flapg.com/ika2/api/login?public", headers={
        "X-Token": id_token,
        "X-Time": timestamp,
        "X-GUID": request_id,
        "X-Hash": hash,
        "X-Ver": "3",
        "X-IID": type
    })
    if flapg_resp.status_code != 200:
        print("Error obtaining f-code from flapg API, aborting... ({})".format(flapg_resp.text))
    return flapg_resp.json()["result"]["f"], flapg_resp.json()["result"]["p1"]

def call_s2s(token, timestamp):
    # I'm not entirely sure what this API does but it gets you a code that you need to move on.
    resp = rsess.post("https://elifessler.com/s2s/api/gen2", data={
        "naIdToken": token,
        "timestamp": timestamp
    }, headers={
        "User-Agent": "testapp/@AT12806379" # This is just me testing things, replace this with a real user agent in a real-world app
    })
    if resp.status_code != 200:
        print("Error obtaining auth hash from Eli Fessler's S2S server, aborting... ({})".format(resp.text))
        sys.exit(1)
    return resp.json()["hash"]

def do_nintendo_oauth():
    # Handles the OAuth process, opening a URL in the user's browser and parses the resulting redirect URI to proceed with login
    verifier, challenge = generate_challenge()
    state = generate_state()

    oauth_uri = "https://accounts.nintendo.com/connect/1.0.0/authorize?state={}&redirect_uri=npf71b963c1b7b6d119://auth&client_id=71b963c1b7b6d119&scope=openid%20user%20user.birthday%20user.mii%20user.screenName&response_type=session_token_code&session_token_code_challenge={}&session_token_code_challenge_method=S256&theme=login_form".format(state, challenge)
    webbrowser.open(oauth_uri)

    print("> もしブラウザが開かない場合は以下のURLを開いてください")
    print(oauth_uri)
    print()
    print("> ログインしたら、「この人にする」ボタンを右クリックし「リンクアドレスをコピー」を選択してここにペーストしてください")
    print()
    oauth_redirect_uri = input("> ").strip()

    redirect_uri_parsed = parse_redirect_uri(oauth_redirect_uri)
    if not redirect_uri_parsed:
        print("Invalid redirect URI, aborting...")
        sys.exit(1)

    session_state, session_token_code, response_state = redirect_uri_parsed
    if state != response_state:
        print("Invalid redirect URI (bad OAuth state), aborting...")
        sys.exit(1)

    return session_token_code, verifier

def login_oauth_session(session_token_code, verifier):
    # Handles the second step of the OAuth process using the information we got from the redirect API
    resp = rsess.post("https://accounts.nintendo.com/connect/1.0.0/api/session_token", data={
        "client_id": nintendo_client_id,
        "session_token_code": session_token_code,
        "session_token_code_verifier": verifier
    }, headers={
        "User-Agent": "OnlineLounge/2.2.0 NASDKAPI Android"
    })
    if resp.status_code != 200:
        print("Error obtaining session token from Nintendo, aborting... ({})".format(resp.text))
        sys.exit(1)

    response_data = resp.json()
    return response_data["session_token"]

def login_nintendo_api(session_token):
    # This properly "logs in" to the Nintendo API getting us a token we can actually use for something practical
    resp = rsess.post("https://accounts.nintendo.com/connect/1.0.0/api/token", data={
        "client_id": nintendo_client_id,
        "session_token": session_token,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer-session-token"
    }, headers={
        "User-Agent": "OnlineLounge/2.2.0 NASDKAPI Android"
    })
    if resp.status_code != 200:
        print("Error obtaining service token from Nintendo, aborting... ({})".format(resp.text))
        sys.exit(1)
    
    response_data = resp.json()
    return response_data["id_token"], response_data["access_token"]

def get_nintendo_account_data(access_token):
    # This fetches information about the currently logged-in user, including locale, country and birthday (needed later)
    resp = rsess.get("https://api.accounts.nintendo.com/2.0.0/users/me", headers={
        "User-Agent": "OnlineLounge/2.2.0 NASDKAPI Android",
        "Authorization": "Bearer {}".format(access_token)
    })
    if resp.status_code != 200:
        print("Error obtaining account data from Nintendo, aborting... ({})".format(resp.text))
        sys.exit(1)
    return resp.json()

def login_switch_web(id_token, nintendo_profile):
    # This logs into the Switch-specific API using a bit of a mess of third-party APIs to get the codes sorted
    timestamp = str(int(datetime.datetime.utcnow().timestamp()))
    request_id = str(uuid.uuid4())
    #request_id2 = str(uuid.uuid4())

    print("> Eli Fessler's S2S APIでハッシュを計算中...")
    nso_hash = call_s2s(id_token, timestamp)

    print("> f-code を flapg APIで計算中...")
    nso_f, registrationToken = call_flapg(id_token, timestamp, request_id, nso_hash, "nso")
    
    print("> Nintendo Switch APIにログイン中...")
    resp = rsess.post("https://api-lp1.znc.srv.nintendo.net/v1/Account/Login", json={
        "parameter": {
            "f": nso_f,
            "naIdToken": id_token,
            "timestamp": timestamp,
            "requestId": request_id,
            "naBirthday": nintendo_profile["birthday"],
            "naCountry": nintendo_profile["country"],
            "language": nintendo_profile["language"]
        }
    }, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "com.nintendo.znca/2.2.0 (Android/10)",
        "X-ProductVersion": "2.2.0",
        "X-Platform": "Android"
    })

    if resp.status_code != 200 or "errorMessage" in resp.json():
        print("Error logging into Switch API, aborting... ({})".format(resp.text))
        sys.exit(1)

    web_token = resp.json()["result"]["webApiServerCredential"]["accessToken"]
    return web_token

def search_friend_code(web_token):
    print("準備完了!")
    friend_code = input("フレンドコードをハイフン付きで入力してください:")
    resp = rsess.post("https://api-lp1.znc.srv.nintendo.net/v3/Friend/GetUserByFriendCode", json={
        "parameter": {
            "friendCode": friend_code
        }
    }, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "com.nintendo.znca/2.2.0 (Android/10)",
        "Authorization": "Bearer {}".format(web_token)
    })
    if resp.status_code != 200 or "errorMessage" in resp.json():
        print("Error searching for friend code, aborting... ({})".format(resp.text))
        sys.exit(1)
    print("{}さんにフレンド申請します".format(resp.json()["result"]["name"]))
    return resp.json()["result"]["nsaId"]

def send_friend_request(web_token, nsa_id):
    resp = rsess.post("https://api-lp1.znc.srv.nintendo.net/v3/FriendRequest/Create", json={
        "parameter": {
            "nsaId": nsa_id
        }
    }, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "com.nintendo.znca/2.2.0 (Android/10)",
        "Authorization": "Bearer {}".format(web_token)
    })
    if resp.status_code != 200 or "errorMessage" in resp.json():
        print("Error sending friend request, aborting... ({})".format(resp.text))
        sys.exit(1)
    print("フレンド申請を送信しました")
    sys.exit(0)

print("STEP 1: アカウント情報を取得するためブラウザを開きます")
session_token_code, verifier = do_nintendo_oauth()

print("STEP 2: Nintendo APIにログイン中...")
session_token = login_oauth_session(session_token_code, verifier)
id_token, access_token = login_nintendo_api(session_token)

print("STEP 3: Switch APIにログイン中...")
nintendo_account_data = get_nintendo_account_data(access_token)
#print(" > Nintendo account data: {}".format(nintendo_account_data))
switch_web_token = login_switch_web(id_token, nintendo_account_data)
#print(" > Switch web token: {}".format(switch_web_token))

nsaId = search_friend_code(switch_web_token)
send_friend_request(switch_web_token, nsaId)