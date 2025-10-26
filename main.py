from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    verify_registration_response,
    verify_authentication_response,
)
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    AuthenticatorTransport,
)

from webauthn.helpers import options_to_json_dict
import secrets
import time
import base64

## JTW IMPORTS 
import jwt
from datetime import datetime, timedelta, timezone




app = FastAPI()

# Add CORS middleware, this is for DEV only and allows https://localhost:8000 to work with  PLEASE SECURE FOR PROD 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

## demo databases in memory only , for prod use a real database
users = {} 
challenges = {} ## for example use redis here

RP_ID = "localhost"
ORIGIN = "http://localhost:8000" 


## here we set the consts needed for the jwt token , THIS IS FOR  A EXAMPLE DEV ONLY , NEVER STORE YOURE JWT_SECRET IN YOURE CODE !!
JWT_SECRET="9ac00d64dcac32c1a07578907f112471db302b29f3d8061b51a04e2b10138e0b"
JWT_ALGORITHM="HS256"
JWT_EXP_MINUTES=15
REFRESH_TOKEN_EXP_MINUTES=7200

##################################################################
### some jwt helper functions that make life easier 

## this part will create the generatic JWT token 
def create_jwt_token(data: dict, exp_minutes: int) -> str:
    """Create a JWT token with expiration."""
    payload = {
        **data,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=exp_minutes),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_access_token(email: str, device_id: str | None = None) -> str:
    """Create a short-lived access JWT.

    The token will include standard claims: `sub` (email) and `type` set to
    "access". If `device_id` is provided it will be included in the payload
    as `device_id`.

    Returns a signed JWT string with the access token expiry.
    """
    data = {"sub": email, "type": "access"}
    if device_id:
        data["device_id"] = device_id
    return create_jwt_token(data, JWT_EXP_MINUTES)

def create_refresh_token(email: str, device_id: str | None = None) -> str:
    """Create a long-lived refresh JWT.

    Payload uses `sub` (email) and `type` set to "refresh". Optionally
    includes `device_id`. Refresh tokens are intended to mint new access
    tokens and have a longer expiry.
    """
    data = {"sub": email, "type": "refresh"}
    if device_id:
        data["device_id"] = device_id
    return create_jwt_token(data, REFRESH_TOKEN_EXP_MINUTES)

## decode jtw 
def decode_jwt_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

## verifiy the token 
def verify_jwt_token(token: str) -> tuple[bool, dict | None]:
    """Verify JWT token and return validity + payload."""
    try:
        payload = decode_jwt_token(token)
        return True, payload
    except jwt.ExpiredSignatureError:
        return False, {"error": "Token has expired"}
    except jwt.InvalidTokenError:
        return False, {"error": "Invalid token"}

########################################################
### step 1 registration 


@app.get("/webauthn/register/options")
def begin_register(email: str):
    """Return WebAuthn registration options for `email`.

    If the user does not yet exist, create a new in-memory user entry.
    The returned options should be sent to the browser to start the
    WebAuthn create() flow. The generated challenge is stored in
    `challenges[email]` for later verification.
    """
    # Check if user already exists, if not create new user
    if email not in users:
        user_id = secrets.token_bytes(16)
        users[email] = {"id": user_id, "credentials": []}
        exclude_credentials = []
    else:
        # Use existing user_id for additional devices
        user_id = users[email]["id"]
        # Exclude existing credentials to prevent duplicate device registrations
        exclude_credentials = [
            PublicKeyCredentialDescriptor(
                id=base64.urlsafe_b64decode(credential["id"] + '=' * (4 - len(credential["id"]) % 4)),
                transports=[AuthenticatorTransport.INTERNAL]
            ) for credential in users[email]["credentials"]
        ]
    ## create the registration options for this registration
    registration_options = generate_registration_options(
        rp_name= "MyWebauthnAPP",
        rp_id = RP_ID,
        ## we include the user_id from above (either new or existing)
        user_id =user_id,
        user_name = email,
        user_display_name= email,
        exclude_credentials=exclude_credentials
    )

    # we save the challenge in our in memory database , we use the challange to prevent replay attacks from outside the orign browser sesion 
    challenges[email] =  registration_options.challenge

    ## we return the registration_options and convert it from json to dict (which fastapi expects, who converts it back to a fastapi json , dunno but it works 
    return options_to_json_dict(registration_options)


################
### step 2 complete the registration

@app.post("/webauthn/register/verify")
async def finish_register(request: Request):
    """Verify WebAuthn registration response and store new credential.

    Expects a JSON body with at least `Email` and `credential` keys. The
    function verifies the attestation using the challenge previously stored
    in `challenges[email]`, then stores the resulting credential (id,
    public_key, counter) in `users[email]["credentials"]`.
    """
    # get the request and make it a json
    body = await request.json()
    # get email from the json
    email = body["Email"]
    # get the challange from the in memory challenges , if there is no challenge get a error (the process must have started)
    registration_challenge = challenges.get(email)
    if not registration_challenge:
        raise HTTPException(400,f"No registration in process for {email}")

    ## now check the request from the device, again we use the body from above 
    registration_verification = verify_registration_response(
        credential = body["credential"],
        expected_challenge=registration_challenge,
        expected_rp_id = RP_ID,
        expected_origin= ORIGIN
    )

    ### now we store the device credential 
    device_credential = {
        ## webauthn doesnt add = as padding to the string if needed causing errors, so we strip away any right side padding if present
        # unsure if this is a dirty fix or just normal ?
        "id": base64.urlsafe_b64encode(registration_verification.credential_id).decode().rstrip('='),
        "public_key": registration_verification.credential_public_key,
        # we also use a counter to prevent replay attacks 
        "counter": registration_verification.sign_count
    }

    ## here we store it in the user table 
    users[email]["credentials"].append(device_credential)
    # and we dont need the challenges anymore so we can delete it
    del challenges[email]

    #finaly we respond with a status registered so the browser knows we did it !
    return {f"status": f"registered user settings {users[email]}"}


########################################
## now we can start the login process 

@app.get("/webauthn/login/options")
def begin_login(email: str):
    """Return WebAuthn authentication options for `email`.

    Builds an allowCredentials list from the user's registered credentials
    and stores the challenge in `challenges[email]` for later verification.
    """
    # first we check if the users is already registered in the database and has at least 1 device in their table
    user = users.get(email)
    if not user or not user["credentials"]:
        raise HTTPException(404, f"there is a issue login in with {email}")
    
    # then we check if the credentials match ! we loop through user[credentials] and append to the publickeycredentialdescriptor
    allow_credentials = [
        PublicKeyCredentialDescriptor(
            id=base64.urlsafe_b64decode(credential["id"] + '=' * (4 - len(credential["id"]) % 4)),
            transports=[AuthenticatorTransport.INTERNAL]
        ) for credential in user["credentials"]
    ]
    
    ## we fill the authenticiaton_options with the RP ID the correct item foudn in the credentials user list
    authentication_options = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow_credentials
    )

    # we add the challlenge to the challenges again to prevent replay attkcs
    challenges[email] = authentication_options.challenge
    ## return the json of the authentication options to the browser
    #return json.loads(options_to_json(authentication_options))
    return options_to_json_dict(authentication_options)


############
## now we can verify the login
@app.post("/webauthn/login/verify")
async def finish_login(request: Request, response: Response):
    """Verify WebAuthn authentication response and issue JWT cookies.

    Expects JSON body with `email` and `credential`. Verifies the assertion
    using the stored challenge and the user's registered credential public
    key. On success, issues `access_token` and `refresh_token` as HTTP-only
    cookies and returns basic login info.
    """
    # we do the same thing as above, get the body and details from the body
    body = await request.json()
    email = body["email"]
    
    # and get the user again from the user db
    user = users.get(email)

    # check to make sure the user exists
    if not user:
        raise HTTPException(400, f"There was a issue with login {email}")


    # lets check the challenge to prevent relay attacks
    authentication_challenge = challenges.get(email)    
    if not authentication_challenge:
        raise HTTPException(400, f"There was a issue with login {email}")

    ## now we can start login with the existing credential
    # store the credential id from the body (already base64url string)
    credential_id = body["credential"]["id"]

    ## find the credential id in the user object from above and ensure the ID  match
    used_device_credential = None
    for credential in user["credentials"]:
        if credential["id"] == credential_id:
            used_device_credential = credential 
            break

    # if no valid credential is found the ask to register again 
    if used_device_credential is None:
        raise HTTPException(401, "no credential registered, please register this device" )

    ## next we verify the signature
    verification = verify_authentication_response(
        credential=body["credential"],
        expected_challenge=authentication_challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        credential_public_key=used_device_credential["public_key"],
        credential_current_sign_count=used_device_credential["counter"],
    )

    # Update de counter van dit device, this will stop replay attacks 
    used_device_credential["counter"] = verification.new_sign_count

    # after succes then the challenge is no longer needed 
    del challenges[email]

    # after successful login create JWT access & refresh tokens and set as httpOnly cookies
    device_id = used_device_credential["id"]
    access_token = create_access_token(email, device_id)
    refresh_token = create_refresh_token(email, device_id)

    # set cookies the secure=false is for DEV ONLY in prod always use https cookies 
    response.set_cookie(
        key="access_token",
        value=access_token,
        max_age=JWT_EXP_MINUTES * 60,
        httponly=True,
        secure=False,
        samesite="lax"
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        max_age=REFRESH_TOKEN_EXP_MINUTES * 60,
        httponly=True,
        secure=False,
        samesite="lax"
    )

    ## finally we return the status code 
    return {
        "status": "ok",
        "user": email,
        "device_id": device_id,
        "login_time": int(time.time())
    }


## JWT cookie-based dependency
def get_current_user_from_cookie(request: Request):
    """Dependency that returns the verified JWT payload from cookie.

    Raises HTTPException(401) when the access token cookie is missing or
    invalid. Returns the decoded token payload (a dict) on success.
    """
    ## get the access token 
    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="Missing access token cookie")

    is_valid, payload = verify_jwt_token(access_token)
    if not is_valid:
        raise HTTPException(status_code=401, detail=payload.get("error", "Invalid token"))
    return payload


## this is a endpoint were we use the jwt token 
@app.get("/protected")
def protected_endpoint(current_user: dict = Depends(get_current_user_from_cookie)): ## here we get the JWT from the cookie 
    """Example protected endpoint that uses the cookie-based JWT dependency.

    Returns a friendly message that includes the `sub` claim from the
    verified access token payload.
    """
    return {"message": f"Hello {current_user.get('sub')} the full payload was {current_user}"}


## here we can logout (and we delete the cookies )
@app.post("/auth/logout")
def logout(response: Response):
    """Clear authentication cookies to log the user out.

    This deletes both `access_token` and `refresh_token` cookies from the
    client. With in-memory sessions you may also want to revoke server-side
    refresh state (not implemented here).
    """
    # clear auth cookies
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"status": "logged_out"}


## this endpoint will create a new 15 minutes access token from the refresh token that is valid for 5 days
@app.post("/auth/refresh")
def refresh_access_token(request: Request, response: Response):
    """Exchange a valid refresh token (cookie) for a new access token.

    This reads the `refresh_token` cookie, verifies it is a refresh token,
    and, if valid, issues a new short-lived access token (cookie). Returns
    the new access token expiry info.
    """
    ## get the refresh token from the browser cookie
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token cookie")

    # verify the jwt token with jtw secret from above to ensure it has a valid signature 
    is_valid, payload = verify_jwt_token(refresh_token)
    if not is_valid:
        error_msg = payload.get("error", "Invalid token") if payload else "Invalid token"
        raise HTTPException(status_code=401, detail=error_msg)
    
    # Ensure token is a refresh token
    if payload and payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token is not a refresh token")

    # get the device id and email  from the payload
    email = payload.get("sub")
    device_id = payload.get("device_id")
    if not email:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    # Issue new access token
    new_access = create_access_token(email, device_id)
    response.set_cookie(
        key="access_token",
        value=new_access,
        max_age=JWT_EXP_MINUTES * 60,
        httponly=True,
        secure=False,
        samesite="lax",
    )

    return {"status": "access_token_refreshed", "user": email}


# Mount static files AFTER all API routes to avoid conflicts
# Also mount the `static` directory at /static so explicit requests
# to /static/... work (e.g. /static/test.html). Keep the root mount
# for SPA-like index fallback behavior.
app.mount("/static", StaticFiles(directory="static"), name="static_files")
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# lets run fastapi 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",      # "filename:app_instance"
        host="localhost",
        port=8000,
        reload=True     # auto-reload when code changes
    )