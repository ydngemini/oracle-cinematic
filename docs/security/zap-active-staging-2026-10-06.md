# ZAP by Checkmarx Scanning Report

ZAP by [Checkmarx](https://checkmarx.com/).


## Summary of Alerts

| Risk Level | Number of Alerts |
| --- | --- |
| High | 0 |
| Medium | 1 |
| Low | 5 |
| Informational | 3 |




## Insights

| Level | Reason | Site | Description | Statistic |
| --- | --- | --- | --- | --- |
| Medium | Exceeded High |  | Percentage of network failures | 70 % |
| Low | Warning |  | ZAP warnings logged - see the zap.log file for details | 5,666    |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of responses with status code 2xx | 24 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of responses with status code 4xx | 72 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of responses with status code 5xx | 3 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type application/json | 95 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type text/html | 1 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with content type text/plain | 2 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of endpoints with method GET | 100 % |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Count of total endpoints | 116    |
| Info | Informational | https://neoh-staging-ksfpn.ondigitalocean.app | Percentage of slow responses | 28 % |







## Alerts

| Name | Risk Level | Number of Instances |
| --- | --- | --- |
| CSP: Failure to Define Directive with No Fallback | Medium | 2 |
| Application Error Disclosure | Low | 3 |
| Cookie with SameSite Attribute None | Low | Systemic |
| Information Disclosure - Debug Error Messages | Low | 3 |
| Strict-Transport-Security Header Not Set | Low | 3 |
| Timestamp Disclosure - Unix | Low | Systemic |
| Loosely Scoped Cookie | Informational | Systemic |
| Re-examine Cache-control Directives | Informational | Systemic |
| Session Management Response Identified | Informational | 114 |




## Alert Detail



### [ CSP: Failure to Define Directive with No Fallback ](https://www.zaproxy.org/docs/alerts/10055/)



##### Medium (High)

### Description

The Content Security Policy fails to define one of the directives that has no fallback. Missing/excluding them is the same as allowing anything.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/documents/access_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/documents/access_id`
  * Method: `GET`
  * Parameter: `content-security-policy`
  * Attack: ``
  * Evidence: `default-src 'none'; frame-ancestors 'none'`
  * Other Info: `The directive(s): form-action is/are among the directives that do not fallback to default-src.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/token
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/token`
  * Method: `GET`
  * Parameter: `content-security-policy`
  * Attack: ``
  * Evidence: `default-src 'none'; frame-ancestors 'none'`
  * Other Info: `The directive(s): form-action is/are among the directives that do not fallback to default-src.`


Instances: 2

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

### [ Application Error Disclosure ](https://www.zaproxy.org/docs/alerts/90022/)



##### Low (Medium)

### Description

This page contains an error/warning message that may disclose sensitive information like the location of the file that produced the unhandled exception. This information can be used to launch further attacks against the web application. The alert could be a false positive if the error message is found inside a documentation page.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `HTTP/1.1 500 Internal Server Error`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `HTTP/1.1 500 Internal Server Error`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments%3Fplan_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments (plan_id)`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `HTTP/1.1 500 Internal Server Error`
  * Other Info: ``


Instances: 3

### Solution

Review the source code of this page. Implement custom error pages. Consider implementing a mechanism to provide a unique error reference/identifier to the client (browser) while logging the details on the server side and not exposing them to the user.

### Reference



#### CWE Id: [ 550 ](https://cwe.mitre.org/data/definitions/550.html)


#### WASC Id: 13

#### Source ID: 3

### [ Cookie with SameSite Attribute None ](https://www.zaproxy.org/docs/alerts/10054/)



##### Low (Medium)

### Description

A cookie has been set with its SameSite attribute set to "none", which means that the cookie can be sent as a result of a 'cross-site' request. The SameSite attribute is an effective counter measure to cross-site request forgery, cross-site script inclusion, and timing attacks.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/links%3Flead_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/links (lead_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `set-cookie: __cf_bm`
  * Other Info: ``

Instances: Systemic


### Solution

Ensure that the SameSite attribute is set to either 'lax' or ideally 'strict' for all cookies.

### Reference


* [ https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site ](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-cookie-same-site)


#### CWE Id: [ 1275 ](https://cwe.mitre.org/data/definitions/1275.html)


#### WASC Id: 13

#### Source ID: 3

### [ Information Disclosure - Debug Error Messages ](https://www.zaproxy.org/docs/alerts/10023/)



##### Low (Medium)

### Description

The response appeared to contain common error messages returned by platforms such as ASP.NET, and Web-servers such as IIS and Apache. You can configure the list of common debug messages.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Internal Server Error`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Internal Server Error`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments%3Fplan_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments (plan_id)`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Internal Server Error`
  * Other Info: ``


Instances: 3

### Solution

Disable debugging messages before pushing to production.

### Reference



#### CWE Id: [ 1295 ](https://cwe.mitre.org/data/definitions/1295.html)


#### WASC Id: 13

#### Source ID: 3

### [ Strict-Transport-Security Header Not Set ](https://www.zaproxy.org/docs/alerts/10035/)



##### Low (High)

### Description

HTTP Strict Transport Security (HSTS) is a web security policy mechanism whereby a web server declares that complying user agents (such as a web browser) are to interact with it using only secure HTTPS connections (i.e. HTTP layered over TLS/SSL). HSTS is an IETF standards track protocol and is specified in RFC 6797.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: ``
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments%3Fplan_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments (plan_id)`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: ``
  * Other Info: ``


Instances: 3

### Solution

Ensure that your web server, application server, load balancer, etc. is configured to enforce Strict-Transport-Security.

### Reference


* [ https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Strict_Transport_Security_Cheat_Sheet.html ](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Strict_Transport_Security_Cheat_Sheet.html)
* [ https://owasp.org/www-community/Security_Headers ](https://owasp.org/www-community/Security_Headers)
* [ https://en.wikipedia.org/wiki/HTTP_Strict_Transport_Security ](https://en.wikipedia.org/wiki/HTTP_Strict_Transport_Security)
* [ https://caniuse.com/stricttransportsecurity ](https://caniuse.com/stricttransportsecurity)
* [ https://datatracker.ietf.org/doc/html/rfc6797 ](https://datatracker.ietf.org/doc/html/rfc6797)


#### CWE Id: [ 319 ](https://cwe.mitre.org/data/definitions/319.html)


#### WASC Id: 15

#### Source ID: 3

### [ Timestamp Disclosure - Unix ](https://www.zaproxy.org/docs/alerts/10096/)



##### Low (Low)

### Description

A timestamp was disclosed by the application/web server. - Unix

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791336305`
  * Other Info: `1791336305, which evaluates to: 2026-10-07 01:25:05.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `1791336292`
  * Other Info: `1791336292, which evaluates to: 2026-10-07 01:24:52.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `1791422692`
  * Other Info: `1791422692, which evaluates to: 2026-10-08 01:24:52.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791336305`
  * Other Info: `1791336305, which evaluates to: 2026-10-07 01:25:05.`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/links%3Flead_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/links (lead_id)`
  * Method: `GET`
  * Parameter: `set-cookie`
  * Attack: ``
  * Evidence: `1791336306`
  * Other Info: `1791336306, which evaluates to: 2026-10-07 01:25:06.`

Instances: Systemic


### Solution

Manually confirm that the timestamp data is not sensitive, and that the data cannot be aggregated to disclose exploitable patterns.

### Reference


* [ https://cwe.mitre.org/data/definitions/200.html ](https://cwe.mitre.org/data/definitions/200.html)


#### CWE Id: [ 497 ](https://cwe.mitre.org/data/definitions/497.html)


#### WASC Id: 13

#### Source ID: 3

### [ Loosely Scoped Cookie ](https://www.zaproxy.org/docs/alerts/90033/)



##### Informational (Low)

### Description

Cookies can be scoped by domain or path. This check is only concerned with domain scope.The domain scope applied to a cookie determines which domains can access it. For example, a cookie can be scoped strictly to a subdomain e.g. www.nottrusted.com, or loosely scoped to a parent domain e.g. nottrusted.com. In the latter case, any subdomain of nottrusted.com can access the cookie. Loosely scoped cookies are common in mega-applications like google.com and live.com. Cookies set from a subdomain like app.foo.bar are transmitted only to that domain by the browser. However, cookies scoped to a parent-level domain may be transmitted to the parent, or any subdomain of the parent.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/links%3Flead_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/links (lead_id)`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id`
  * Method: `GET`
  * Parameter: ``
  * Attack: ``
  * Evidence: `Domain=ondigitalocean.app`
  * Other Info: `The origin domain used for comparison was:
neoh-staging-ksfpn.ondigitalocean.app
Cookie name: __cf_bm
`

Instances: Systemic


### Solution

Always scope cookies to a FQDN (Fully Qualified Domain Name).

### Reference


* [ https://datatracker.ietf.org/doc/html/rfc6265#section-4.1 ](https://datatracker.ietf.org/doc/html/rfc6265#section-4.1)
* [ https://owasp.org/www-project-web-security-testing-guide/v41/4-Web_Application_Security_Testing/06-Session_Management_Testing/02-Testing_for_Cookies_Attributes.html ](https://owasp.org/www-project-web-security-testing-guide/v41/4-Web_Application_Security_Testing/06-Session_Management_Testing/02-Testing_for_Cookies_Attributes.html)
* [ https://code.google.com/archive/p/browsersec/wikis/Part2.wiki ](https://code.google.com/archive/p/browsersec/wikis/Part2.wiki)


#### CWE Id: [ 565 ](https://cwe.mitre.org/data/definitions/565.html)


#### WASC Id: 15

#### Source ID: 3

### [ Re-examine Cache-control Directives ](https://www.zaproxy.org/docs/alerts/10015/)



##### Informational (Low)

### Description

The cache-control header has not been set properly or is missing, allowing the browser and proxies to cache content. For static assets like css, js, or image files this might be intended, however, the resources should be reviewed to ensure that no sensitive content will be cached.

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/listings
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/listings`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/profile
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/profile`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/csrf`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/policy-acceptance`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: `cache-control`
  * Attack: ``
  * Evidence: `private`
  * Other Info: ``

Instances: Systemic


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

* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/invitations
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/invitations`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/mls/entitlements
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/mls/entitlements`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/setup
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/setup`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/team
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/brokerage/team`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/clients/client_id/intent`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/command-center%3Flookback_hours=24
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/command-center (lookback_hours)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands%3Fstate=Oklahoma&limit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands (limit,state)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/approvals%3Fstatus=&limit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/approvals (limit,status)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/command_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/providers
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/providers`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/compliance/checklist/transaction_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/compliance/checklist/transaction_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/compliance/documents/state_code
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/compliance/documents/state_code`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents%3Flimit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents/document_id%3Finclude_draft=false
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents/document_id (include_draft)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents/document_id/download%3Fexpires_in=300
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/documents/document_id/download (expires_in)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces%3Flimit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces/workspace_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces/workspace_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces/workspace_id/download
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/draft-workspaces/workspace_id/download`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/documents/access_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/documents/access_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/status
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/govinfo/status`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/pdf-library
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/pdf-library`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/pdf-library/registered/source_key/download
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/pdf-library/registered/source_key/download`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/policy
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/policy`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/synthesis-artifacts%3Fclient_id=client_id&state_code=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/synthesis-artifacts (client_id,state_code)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/synthesis-artifacts/artifact_id/download%3Fexpiration_seconds=3600
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/synthesis-artifacts/artifact_id/download (expiration_seconds)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates/library
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates/library`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates/library/template_key/pdf
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/contracts/templates/library/template_key/pdf`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients%3Ftype=all&stage=&tag=&q=&score_min=&assignee=&sort=recent
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients (assignee,q,score_min,sort,stage,tag,type)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/automation
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/automation`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/interactions%3Flimit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/interactions (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/notes
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/notes`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/showings%3Flimit=20
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/showings (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/timeline%3Flimit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/client_id/timeline (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/segments
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/clients/segments`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/comms/threads
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/comms/threads`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/comms/threads/thread_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/comms/threads/thread_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/contacts%3Fq=&limit=100&cursor=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/contacts (cursor,limit,q)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/contacts/contact_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/contacts/contact_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/floorplan%3Flead_id=&listing_id=&revision=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/floorplan (lead_id,listing_id,revision)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/floorplan/floorplan_id/revisions%3Flimit=25
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/floorplan/floorplan_id/revisions (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/intake/questions/buyer
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/intake/questions/buyer`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/listings
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/listings`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/media%3Flead_id=&listing_id=&kind=photo
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/media (kind,lead_id,listing_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/profile
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/profile`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-tour%3Flead_id=&listing_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-tour (lead_id,listing_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-view%3Flead_id=&listing_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-view (lead_id,listing_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-view/resolve%3Faddress=688%2520Zaproxy%2520Ridge
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/property-view/resolve (address)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/reconstruction-jobs/job_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/reconstruction-jobs/job_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/reconstruction-jobs/job_id/diagnostics
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/reconstruction-jobs/job_id/diagnostics`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/agents
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/agents`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/connectors
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/connectors`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/events%3Fstatus=&source_key=&cursor=&limit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/events (cursor,limit,source_key,status)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/metrics%3Fdays=30
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/metrics (days)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/rules
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/routing/rules`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/space%3Flead_id=&listing_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/space (lead_id,listing_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/tasks%3Fstatus=&client_id=&due=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/crm/tasks (client_id,due,status)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/leads/lead_id/dossier
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/leads/lead_id/dossier`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/agent/agent_id/status
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/agent/agent_id/status`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/reciprocity/from_state/to_state
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/reciprocity/from_state/to_state`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/requirements/state_code%3Flicense_type=salesperson
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/licensing/requirements/state_code (license_type)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace%3Fstate_code=&limit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace (limit,state_code)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace/buyers/profiles%3Flimit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace/buyers/profiles (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace/publications%3Flimit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/marketplace/publications (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/media/media_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/media/media_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/messaging/business-number
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/messaging/business-number`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/messaging/business/registration
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/messaging/business/registration`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/health
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/health`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/listing/listing_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/listing/listing_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/listings/listing_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/listings/listing_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/public-records%3Fcity=East%2520Romaineburgh&state=Oklahoma&zip=&min_price=&max_price=&beds=&q=&page=1
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/public-records (beds,city,max_price,min_price,page,q,state,zip)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/public-records/record_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/public-records/record_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/regions%3Fstate_code=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/regions (state_code)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/regions/mls_id/status
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/regions/mls_id/status`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/search%3Fcity=East%2520Romaineburgh&state=Oklahoma&zip=&min_price=&max_price=&beds=&property_type=&page=1
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/mls/search (beds,city,max_price,min_price,page,property_type,state,zip)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/opportunities
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/opportunities`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/opportunities/perception
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/opportunities/perception`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/summary
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/summary`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions%3Fstatus=&property_source=&client_id=&limit=100&offset=0
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions (client_id,limit,offset,property_source,status)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions/transaction_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions/transaction_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions/transaction_id/offers
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/portfolio/transactions/transaction_id/offers`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/public/property-upload/token
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/public/property-upload/token`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/agent/work-queue%3Fq=&stage=&limit=100
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/agent/work-queue (limit,q,stage)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/capabilities
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/capabilities`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments%3Fplan_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/enrollments (plan_id)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/plan_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/plans/plan_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/providers
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sales/providers`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/search%3Fq=q&types=types&limit=10
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/search (limit,q,types)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/search/recent%3Flimit=8
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/search/recent (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sites%3Flimit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sites (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id/collaborators
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id/collaborators`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id/funnel%3Fdays=90
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/site_id/funnel (days)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/templates
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/sites/templates`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/advertising-rules%3Fcategory=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/advertising-rules (category)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/contracts%3Fproperty_type=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/contracts (property_type)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/document-library
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/document-library`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/forms%3Fform_type=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/states/state_code/forms (form_type)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/status
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/status`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/calls%3Flimit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/calls (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/token
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/agent/token`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/calls%3Flimit=50
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/calls (limit)`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/calls/call_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/calls/call_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/routes
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/api/telephony/routes`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/auth/session
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/auth/session`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/dossier
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/dossier`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/media/media_id`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/session/token
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/session/token`
  * Method: `GET`
  * Parameter: `__cf_bm`
  * Attack: ``
  * Evidence: `__cf_bm`
  * Other Info: `cookie:__cf_bm`
* URL: https://neoh-staging-ksfpn.ondigitalocean.app/portal/links%3Flead_id=
  * Node Name: `https://neoh-staging-ksfpn.ondigitalocean.app/portal/links (lead_id)`
  * Method: `GET`
  * Parameter: `lead_id`
  * Attack: ``
  * Evidence: `lead_id`
  * Other Info: `url:lead_id`


Instances: 114

### Solution

This is an informational alert rather than a vulnerability and so there is nothing to fix.

### Reference


* [ https://www.zaproxy.org/docs/desktop/addons/authentication-helper/session-mgmt-id/ ](https://www.zaproxy.org/docs/desktop/addons/authentication-helper/session-mgmt-id/)



#### Source ID: 3


