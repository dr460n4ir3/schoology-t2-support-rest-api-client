"""Hide personal data and secrets in API responses, by field name.

Schoology and OneRoster responses are JSON, so personal fields can be found by name (email, name_first,
givenName, birthDate, phone, address...). Their values are replaced with HIDDEN:
  * passwords, tokens and secrets: always hidden, everywhere;
  * personal fields: hidden on screen until the user clicks "Show personal data", and always hidden in anything
    copied, downloaded, saved or sent to an AI.
Ids (user id, sourcedId, school uid) are not personal data on their own and stay visible: support work needs them.
"""

from __future__ import annotations

import json
import re

HIDDEN = "●●●●●●"

# compared after lower-casing and removing "_", "-", "." and spaces
_SECRET_EXACT = {"password", "passwd", "pwd", "secret", "token", "accesstoken", "refreshtoken", "idtoken",
                 "clientsecret", "consumersecret", "tokensecret", "oauthtokensecret", "apikey", "privatekey",
                 "authorization", "cookie", "setcookie", "sessionid"}
_SECRET_PARTS = ("password", "secret", "accesstoken", "refreshtoken", "apikey", "privatekey")

_PII_EXACT = {
    # names
    "firstname", "lastname", "middlename", "givenname", "familyname", "fullname", "displayname",
    "namefirst", "namelast", "namemiddle", "namedisplay", "namefirstpreferred", "namelastpreferred",
    "nametitle", "nickname", "preferredname", "preferredfirstname", "preferredlastname", "preferredmiddlename",
    "preferredgivenname", "preferredfamilyname", "maidenname", "username", "login", "loginname",
    # contact
    "email", "emails", "primaryemail", "emailaddress", "alternateemail", "sms", "phone", "phones", "phonenumber",
    "mobile", "mobilephone", "cellphone", "homephone", "workphone", "fax",
    # address
    "address", "addresses", "street", "streetaddress", "address1", "address2", "addressline1", "addressline2",
    "city", "postalcode", "zip", "zipcode", "postcode",
    # dates and identity
    "birthdate", "birthday", "birthdaydate", "dob", "dateofbirth", "ssn", "socialsecuritynumber", "gender", "sex",
    "picture", "pictureurl", "avatar", "photo", "photourl",
}
_PII_PARTS = ("email", "phone", "birth", "address", "firstname", "lastname", "givenname", "familyname",
              "middlename", "preferredname")
# keys whose names contain a personal word but hold no personal data
_NOT_PII = {"emailverified", "phonetype", "addresstype", "emailtype", "schoolname", "coursename", "sectionname",
            "orgname", "buildingname", "titlename", "gradename", "termname", "classname", "groupname", "typename",
            "filename", "hostname", "pathname"}


def _norm(key) -> str:
    return re.sub(r"[\s_\-.]+", "", str(key)).lower()


def is_secret_field(key) -> bool:
    k = _norm(key)
    return k in _SECRET_EXACT or any(p in k for p in _SECRET_PARTS)


def is_personal_field(key) -> bool:
    k = _norm(key)
    if k in _NOT_PII:
        return False
    return k in _PII_EXACT or any(p in k for p in _PII_PARTS)


def _hide_all(value):
    """Everything under a personal field (a name object, a list of emails...) is hidden, keeping its shape."""
    if isinstance(value, dict):
        return {k: _hide_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hide_all(v) for v in value]
    if value is None or isinstance(value, bool) or value == "":
        return value
    return HIDDEN


def mask(data, hide_personal: bool = True):
    """A copy of parsed JSON with secrets (and, unless hide_personal=False, personal fields) hidden."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if is_secret_field(k) or (hide_personal and is_personal_field(k)):
                out[k] = _hide_all(v)
            else:
                out[k] = mask(v, hide_personal)
        # {"key": "email", "value": "x@y"} / {"name": "password", "value": ...} pairs
        label = next((data.get(n) for n in ("key", "name", "field", "type") if isinstance(data.get(n), str)), None)
        if label and (is_secret_field(label) or (hide_personal and is_personal_field(label))):
            for field in ("value", "identifier"):  # OneRoster userIds: {"type": "email", "identifier": ...}
                if field in out:
                    out[field] = _hide_all(data[field])
        return out
    if isinstance(data, list):
        return [mask(v, hide_personal) for v in data]
    return data


_XML_FIELD = re.compile(r"<([A-Za-z_][\w.\-]*)(\s[^>]*)?>([^<]*)</\1>")


def mask_text(text: str, hide_personal: bool = True) -> str:
    """Mask a response body: JSON by field name (re-formatted), XML elements by tag name, anything else as is."""
    if not text:
        return text or ""
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            return json.dumps(mask(data, hide_personal), indent=2, ensure_ascii=False)
    if stripped.startswith("<"):
        def repl(m):
            tag = m.group(1).split(":")[-1]
            if m.group(3).strip() and (is_secret_field(tag) or (hide_personal and is_personal_field(tag))):
                return f"<{m.group(1)}{m.group(2) or ''}>{HIDDEN}</{m.group(1)}>"
            return m.group(0)
        return _XML_FIELD.sub(repl, text)
    return text


def has_personal_data(text: str) -> bool:
    return mask_text(text, True) != mask_text(text, False)
