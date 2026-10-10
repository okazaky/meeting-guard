# -*- coding: utf-8 -*-
"""HTTP helpers. Redirects are never followed and environment proxies are
ignored (credentials must not be re-sent anywhere else)."""
import json
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused",
                                     headers, None)


# ProxyHandler({}) ignores http(s)_proxy environment variables, so Authorization
# headers (Google / Twilio / ntfy) are never handed to an environment-chosen proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def open_request(req, timeout):
    return OPENER.open(req, timeout=timeout)


def request_json(req, timeout):
    with open_request(req, timeout) as r:
        txt = r.read().decode("utf-8")
    return json.loads(txt) if txt else {}
