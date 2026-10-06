# ZAP Scanning Report

ZAP by [Checkmarx](https://checkmarx.com/).


## Summary of Alerts

| Risk Level | Number of Alerts |
| --- | --- |
| High | 0 |
| Medium | 1 |
| Low | 6 |
| Informational | 7 |




## Insights

| Level | Reason | Site | Description | Statistic |
| --- | --- | --- | --- | --- |
| Low | Warning |  | ZAP warnings logged - see the zap.log file for details | 1    |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of responses with status code 2xx | 100 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type application/javascript | 45 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type application/octet-stream | 9 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type image/png | 9 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type image/svg+xml | 9 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type text/css | 9 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type text/html | 18 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with method GET | 100 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Count of total endpoints | 11    |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of slow responses | 100 % |







## Alerts

| Name | Risk Level | Number of Instances |
| --- | --- | --- |
| CSP: Wildcard Directive | Medium | 3 |
| Cookie with SameSite Attribute None | Low | 1 |
| Cookie without SameSite Attribute | Low | 3 |
| Cross-Origin-Embedder-Policy Header Missing or Invalid | Low | 3 |
| Cross-Origin-Opener-Policy Header Missing or Invalid | Low | 3 |
| Cross-Origin-Resource-Policy Header Missing or Invalid | Low | 3 |
| Timestamp Disclosure - Unix | Low | 3 |
| Information Disclosure - Suspicious Comments | Informational | 1 |
| Loosely Scoped Cookie | Informational | 3 |
| Modern Web Application | Informational | 3 |
| Non-Storable Content | Informational | Systemic |
| Re-examine Cache-control Directives | Informational | 3 |
| Session Management Response Identified | Informational | 3 |
| Storable and Cacheable Content | Informational | 4 |




## Alert Detail



### [ CSP: Wildcard Directive ](https://www.zaproxy.org/docs/alerts/10055/)



##### Medium (High)

### Description

Content Security Policy (CSP) is an added layer of security that helps to detect and mitigate certain types of attacks. Including (but not limited to) Cross Site Scripting (XSS), and data injection attacks. These attacks are used for everything from data theft to site defacement or distribution of malware. CSP provides a set of standard HTTP headers that allow website owners to declare approved sources of content that browsers should be allowed to load on that page — covered types are JavaScript, CSS, HTML frames, fonts, images and embeddable objects such as Java applets, ActiveX, audio and video files.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `content-security-policy`
  * Attack: ``
  * Evidence: `default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`
  * Other Info: `The following directives either allow wildcard sources (or ancestors), are not defined, or are overly broadly defined:
media-src
The response contained 2 Content-Security-Policy policies (header and/or meta). They were analyzed together using browser-style intersection (a resource is allowed only if every policy allows it):
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests
default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `content-security-policy`
  * Attack: ``
  * Evidence: `default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`
  * Other Info: `The following directives either allow wildcard sources (or ancestors), are not defined, or are overly broadly defined:
media-src
The response contained 2 Content-Security-Policy policies (header and/or meta). They were analyzed together using browser-style intersection (a resource is allowed only if every policy allows it):
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests
default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `content-security-policy`
  * Attack: ``
  * Evidence: `default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`
  * Other Info: `The following directives either allow wildcard sources (or ancestors), are not defined, or are overly broadly defined:
media-src
The response contained 2 Content-Security-Policy policies (header and/or meta). They were analyzed together using browser-style intersection (a resource is allowed only if every policy allows it):
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://neohrs.com wss://neohrs.com https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests
default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'self' https://accounts.google.com; script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://streetviewpixels-pa.googleapis.com; font-src 'self' data:; media-src 'self' blob: https:; worker-src 'self' blob:; connect-src 'self' https://maps.googleapis.com https://*.googleapis.com https://eventgw.twilio.com wss://voice-js.roaming.twilio.com https://media.twiliocdn.com https://sdk.twilio.com; upgrade-insecure-requests`


Instances: 3

### Solution

Ensure that your web server, application server, load balancer, etc. is properly configured to set the Content-Security-Policy header.

### Reference


* [ https://www.w3.org/TR/CSP/ ](https://www.w3.org/TR/CSP/)
* [ https://caniuse.com/#search=content+security+policy ](https://caniuse.com/#search=content+security+policy)
* [ https://content-security-policy.com/ ](https://content-security-policy.com/)
* [ https://github.com/HtmlUnit/htmlunit-csp ](https://github.com/HtmlUnit/htmlunit-csp)
* [ https://web.dev/articles/csp#resource-options ](https://web.dev/articles/csp#resource-options)


#### CWE Id: [ 693 ](https://cwe.mitre.org/data/definitions/693.html)


#### WASC Id: 15

#### Source ID: 3

### [ Cookie with SameSite Attribute None ](https://www.zaproxy.org/docs/alerts/10054/)



##### Low (Medium)

### Description

A cookie has been set with its SameSite attribute set to "none", which means that the cookie can be sent as a result of a 'cross-site' request. The SameSite attribute is an effective counter measure to cross-site request forgery, cross-site script inclusion, and timing attacks.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``


Instances: 1

### Solution

Ensure that the SameSite attribute is set to either 'lax' or ideally 'strict' for all cookies.

### Reference


* [ https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site ](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site)


#### CWE Id: [ 1275 ](https://cwe.mitre.org/data/definitions/1275.html)


#### WASC Id: 13

#### Source ID: 3

### [ Cookie without SameSite Attribute ](https://www.zaproxy.org/docs/alerts/10054/)



##### Low (Medium)

### Description

A cookie has been set without the SameSite attribute, which means that the cookie can be sent as a result of a 'cross-site' request. The SameSite attribute is an effective counter measure to cross-site request forgery, cross-site script inclusion, and timing attacks.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``


Instances: 3

### Solution

Ensure that the SameSite attribute is set to either 'lax' or ideally 'strict' for all cookies.

### Reference


* [ https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site ](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site)


#### CWE Id: [ 1275 ](https://cwe.mitre.org/data/definitions/1275.html)


#### WASC Id: 13

#### Source ID: 3

### [ Cross-Origin-Embedder-Policy Header Missing or Invalid ](https://www.zaproxy.org/docs/alerts/90004/)



##### Low (Medium)

### Description

Cross-Origin-Embedder-Policy header is a response header that prevents a document from loading any cross-origin resources that don't explicitly grant the document permission (using CORP or CORS).

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `Cross-Origin-Embedder-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `Cross-Origin-Embedder-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `Cross-Origin-Embedder-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``


Instances: 3

### Solution

Ensure that the application/web server sets the Cross-Origin-Embedder-Policy header appropriately, and that it sets the Cross-Origin-Embedder-Policy header to 'require-corp' for documents.
If possible, ensure that the end user uses a standards-compliant and modern web browser that supports the Cross-Origin-Embedder-Policy header (https://caniuse.com/mdn-http_headers_cross-origin-embedder-policy).

### Reference


* [ https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Embedder-Policy ](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Embedder-Policy)


#### CWE Id: [ 693 ](https://cwe.mitre.org/data/definitions/693.html)


#### WASC Id: 14

#### Source ID: 3

### [ Cross-Origin-Opener-Policy Header Missing or Invalid ](https://www.zaproxy.org/docs/alerts/90004/)



##### Low (Medium)

### Description

Cross-Origin-Opener-Policy header is a response header that allows a site to control if others included documents share the same browsing context. Sharing the same browsing context with untrusted documents might lead to data leak.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `Cross-Origin-Opener-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `Cross-Origin-Opener-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `Cross-Origin-Opener-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``


Instances: 3

### Solution

Ensure that the application/web server sets the Cross-Origin-Opener-Policy header appropriately, and that it sets the Cross-Origin-Opener-Policy header to 'same-origin' for documents.
'same-origin-allow-popups' is considered as less secured and should be avoided.
If possible, ensure that the end user uses a standards-compliant and modern web browser that supports the Cross-Origin-Opener-Policy header (https://caniuse.com/mdn-http_headers_cross-origin-opener-policy).

### Reference


* [ https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Opener-Policy ](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Opener-Policy)


#### CWE Id: [ 693 ](https://cwe.mitre.org/data/definitions/693.html)


#### WASC Id: 14

#### Source ID: 3

### [ Cross-Origin-Resource-Policy Header Missing or Invalid ](https://www.zaproxy.org/docs/alerts/90004/)



##### Low (Medium)

### Description

Cross-Origin-Resource-Policy header is an opt-in header designed to counter side-channels attacks like Spectre. Resource should be specifically set as shareable amongst different origins.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `Cross-Origin-Resource-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `Cross-Origin-Resource-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `Cross-Origin-Resource-Policy`
  * Attack: ``
  * Evidence: ``
  * Other Info: ``


Instances: 3

### Solution

Ensure that the application/web server sets the Cross-Origin-Resource-Policy header appropriately, and that it sets the Cross-Origin-Resource-Policy header to 'same-origin' for all web pages.
'same-site' is considered as less secured and should be avoided.
If resources must be shared, set the header to 'cross-origin'.
If possible, ensure that the end user uses a standards-compliant and modern web browser that supports the Cross-Origin-Resource-Policy header (https://caniuse.com/mdn-http_headers_cross-origin-resource-policy).

### Reference


* [ https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Embedder-Policy ](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cross-Origin-Embedder-Policy)


#### CWE Id: [ 693 ](https://cwe.mitre.org/data/definitions/693.html)


#### WASC Id: 14

#### Source ID: 3

### [ Timestamp Disclosure - Unix ](https://www.zaproxy.org/docs/alerts/10096/)



##### Low (Low)

### Description

A timestamp was disclosed by the application/web server. - Unix

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791317387`
  * Other Info: `1791317387, which evaluates to: 2026-10-06 20:09:47.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791317387`
  * Other Info: `1791317387, which evaluates to: 2026-10-06 20:09:47.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791317387`
  * Other Info: `1791317387, which evaluates to: 2026-10-06 20:09:47.`


Instances: 3

### Solution

Manually confirm that the timestamp data is not sensitive, and that the data cannot be aggregated to disclose exploitable patterns.

### Reference


* [ https://cwe.mitre.org/data/definitions/200.html ](https://cwe.mitre.org/data/definitions/200.html)


#### CWE Id: [ 497 ](https://cwe.mitre.org/data/definitions/497.html)


#### WASC Id: 13

#### Source ID: 3

### [ Information Disclosure - Suspicious Comments ](https://www.zaproxy.org/docs/alerts/10027/)



##### Informational (Medium)

### Description

The response appears to contain suspicious comments which may help an attacker.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/theme-init.js
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/theme-init.js`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `ts (loaded blocking from <head>).`
  * Other Info: `The following pattern was used: \bFROM\b and was detected in likely comment: "// Stamp the chosen theme before any CSS paints (loaded blocking from <head>).", see evidence field for the suspicious comment/snippet.`


Instances: 1

### Solution

Remove all comments that return information that may help an attacker and fix any underlying problems they refer to.

### Reference



#### CWE Id: [ 615 ](https://cwe.mitre.org/data/definitions/615.html)


#### WASC Id: 13

#### Source ID: 3

### [ Loosely Scoped Cookie ](https://www.zaproxy.org/docs/alerts/90033/)



##### Informational (Low)

### Description

Cookies can be scoped by domain or path. This check is only concerned with domain scope.The domain scope applied to a cookie determines which domains can access it. For example, a cookie can be scoped strictly to a subdomain e.g. www.nottrusted.com, or loosely scoped to a parent domain e.g. nottrusted.com. In the latter case, any subdomain of nottrusted.com can access the cookie. Loosely scoped cookies are common in mega-applications like google.com and live.com. Cookies set from a subdomain like app.foo.bar are transmitted only to that domain by the browser. However, cookies scoped to a parent-level domain may be transmitted to the parent, or any subdomain of the parent.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`


Instances: 3

### Solution

Always scope cookies to a FQDN (Fully Qualified Domain Name).

### Reference


* [ https://datatracker.ietf.org/doc/html/rfc6265#section-4.1 ](https://datatracker.ietf.org/doc/html/rfc6265#section-4.1)
* [ https://owasp.org/www-project-web-security-testing-guide/v41/4-Web_Application_Security_Testing/06-Session_Management_Testing/02-Testing_for_Cookies_Attributes.html ](https://owasp.org/www-project-web-security-testing-guide/v41/4-Web_Application_Security_Testing/06-Session_Management_Testing/02-Testing_for_Cookies_Attributes.html)
* [ https://code.google.com/archive/p/browsersec/wikis/Part2.wiki ](https://code.google.com/archive/p/browsersec/wikis/Part2.wiki)


#### CWE Id: [ 565 ](https://cwe.mitre.org/data/definitions/565.html)


#### WASC Id: 15

#### Source ID: 3

### [ Modern Web Application ](https://www.zaproxy.org/docs/alerts/10109/)



##### Informational (Medium)

### Description

The application appears to be a modern web application. If you need to explore it automatically then the Client Spider may well be more effective than the standard one.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `<script src="/theme-init.js"></script>`
  * Other Info: `No links have been found while there are scripts, which is an indication that this is a modern web application.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `<script src="/theme-init.js"></script>`
  * Other Info: `No links have been found while there are scripts, which is an indication that this is a modern web application.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `<script src="/theme-init.js"></script>`
  * Other Info: `No links have been found while there are scripts, which is an indication that this is a modern web application.`


Instances: 3

### Solution

This is an informational alert and so no changes are required.

### Reference




#### Source ID: 3

### [ Non-Storable Content ](https://www.zaproxy.org/docs/alerts/10049/)



##### Informational (Medium)

### Description

The response contents are not storable by caching components such as proxy servers. If the response does not contain sensitive, personal or user-specific information, it may benefit from being stored and cached, to improve performance.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `no-store`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/oracle-192.png
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/oracle-192.png`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `no-store`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `no-store`
  * Other Info: ``

Instances: Systemic


### Solution

The content may be marked as storable by ensuring that the following conditions are satisfied:
The request method must be understood by the cache and defined as being cacheable ("GET", "HEAD", and "POST" are currently defined as cacheable)
The response status code must be understood by the cache (one of the 1XX, 2XX, 3XX, 4XX, or 5XX response classes are generally understood)
The "no-store" cache directive must not appear in the request or response header fields
For caching by "shared" caches such as "proxy" caches, the "private" response directive must not appear in the response
For caching by "shared" caches such as "proxy" caches, the "Authorization" header field must not appear in the request, unless the response explicitly allows it (using one of the "must-revalidate", "public", or "s-maxage" Cache-Control response directives)
In addition to the conditions above, at least one of the following conditions must also be satisfied by the response:
It must contain an "Expires" header field
It must contain a "max-age" response directive
For "shared" caches such as "proxy" caches, it must contain a "s-maxage" response directive
It must contain a "Cache Control Extension" that allows it to be cached
It must have a status code that is defined as cacheable by default (200, 203, 204, 206, 300, 301, 404, 405, 410, 414, 501).

### Reference


* [ https://datatracker.ietf.org/doc/html/rfc7234 ](https://datatracker.ietf.org/doc/html/rfc7234)
* [ https://datatracker.ietf.org/doc/html/rfc7231 ](https://datatracker.ietf.org/doc/html/rfc7231)
* [ https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html ](https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html)


#### CWE Id: [ 524 ](https://cwe.mitre.org/data/definitions/524.html)


#### WASC Id: 13

#### Source ID: 3

### [ Re-examine Cache-control Directives ](https://www.zaproxy.org/docs/alerts/10015/)



##### Informational (Low)

### Description

The cache-control header has not been set properly or is missing, allowing the browser and proxies to cache content. For static assets like css, js, or image files this might be intended, however, the resources should be reviewed to ensure that no sensitive content will be cached.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `no-store, must-revalidate`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `no-store, must-revalidate`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `no-store, must-revalidate`
  * Other Info: ``


Instances: 3

### Solution

For secure content, ensure the cache-control HTTP header is set with "no-cache, no-store, must-revalidate". If an asset should be cached consider setting the directives "public, max-age, immutable".

### Reference


* [ https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#web-content-caching ](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#web-content-caching)
* [ https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control ](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control)
* [ https://grayduck.mn/2021/09/13/cache-control-recommendations/ ](https://grayduck.mn/2021/09/13/cache-control-recommendations/)


#### CWE Id: [ 525 ](https://cwe.mitre.org/data/definitions/525.html)


#### WASC Id: 13

#### Source ID: 3

### [ Session Management Response Identified ](https://www.zaproxy.org/docs/alerts/10112/)



##### Informational (Medium)

### Description

The given response has been identified as containing a session management token. The 'Other Info' field contains a set of header tokens that can be used in the Header Based Session Management Method. If the request is in a context which has a Session Management Method set to "Auto-Detect" then this rule will change the session management to use the tokens identified.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/robots.txt`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/sitemap.xml`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`


Instances: 3

### Solution

This is an informational alert rather than a vulnerability and so there is nothing to fix.

### Reference


* [ https://www.zaproxy.org/docs/desktop/addons/authentication-helper/session-mgmt-id/ ](https://www.zaproxy.org/docs/desktop/addons/authentication-helper/session-mgmt-id/)



#### Source ID: 3

### [ Storable and Cacheable Content ](https://www.zaproxy.org/docs/alerts/10049/)



##### Informational (Medium)

### Description

The response contents are storable by caching components such as proxy servers, and may be retrieved directly from the cache, rather than from the origin server by the caching servers, in response to similar requests from other users. If the response data is sensitive, personal or user-specific, this may result in sensitive information being leaked. In some cases, this may even result in a user gaining complete control of the session of another user, depending on the configuration of the caching components in use in their environment. This is primarily an issue where "shared" caching servers such as "proxy" caches are configured on the local network. This configuration is typically found in corporate or educational environments, for instance.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/assets/createLucideIcon-DkA25d1X.js
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/assets/createLucideIcon-DkA25d1X.js`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `max-age=31536000`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/assets/index-D0bYMihr.css
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/assets/index-D0bYMihr.css`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `max-age=31536000`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/assets/jsx-runtime-BhxXKzgZ.js
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/assets/jsx-runtime-BhxXKzgZ.js`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `max-age=31536000`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/assets/rolldown-runtime-CNC7AqOf.js
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/assets/rolldown-runtime-CNC7AqOf.js`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `max-age=31536000`
  * Other Info: ``


Instances: 4

### Solution

Validate that the response does not contain sensitive, personal or user-specific information. If it does, consider the use of the following HTTP response headers, to limit, or prevent the content being stored and retrieved from the cache by another user:
Cache-Control: no-cache, no-store, must-revalidate, private
Pragma: no-cache
Expires: 0
This configuration directs both HTTP 1.0 and HTTP 1.1 compliant caching servers to not store the response, and to not retrieve the response (without validation) from the cache, in response to a similar request.

### Reference


* [ https://datatracker.ietf.org/doc/html/rfc7234 ](https://datatracker.ietf.org/doc/html/rfc7234)
* [ https://datatracker.ietf.org/doc/html/rfc7231 ](https://datatracker.ietf.org/doc/html/rfc7231)
* [ https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html ](https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html)


#### CWE Id: [ 524 ](https://cwe.mitre.org/data/definitions/524.html)


#### WASC Id: 13

#### Source ID: 3


