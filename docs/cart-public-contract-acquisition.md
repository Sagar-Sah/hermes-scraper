# Public cart contract acquisition

On 2026-09-26 at 06:47:44 UTC, one ordinary unauthenticated GET was made to
`https://www.printograph.com/products/business-cards/order`. It returned HTTP
302 to `/user/authenticate` and an empty response body. The redirect was not
followed. Its transient view-state query was omitted from the retained report.

The four-request maximum was respected: **1 request used, 0 cart API requests,
0 mutations**. No browser session, cookies, account credential, or cart state
was used. The acquisition helper disables redirects/retries and uses a new
cookie-free client for each invocation, with at least 1.1 seconds between any
successive request starts. No second request was made.

| Evidence | Value |
| --- | --- |
| Response status | 302 |
| Response body bytes | 0 |
| SHA-256 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| Local manifest | `work/cart-contracts/manifest.json` |
| Empty response artifact | `work/cart-contracts/public-1.body` |

The public response could not establish the actual
`window.vueCartAndCheckoutEnabled` value, select `/shop/cart.html` versus
`/store/shopping-cart.html`, or expose HTML-declared cart script URLs. Neither
cart page nor any guessed script path was requested.

The existing offline findings in `cart-line-lifecycle.md` therefore remain:
`removeCartItems` is an arbitrary-body DELETE wrapper without a saved caller,
and successful add responses are consumed for IDs without an independently
established line-price recalculation/readback schema. No targeted-delete body,
price readback schema, or price-independence claim can be added from this empty
response.

The next required artifact is the **actual source-selected cart page's declared
entry script**, obtained through an existing authorized normal session or an
already saved public artifact. This acquisition did not access that session.
The script must establish the targeted removal caller and line-price consumer
before temporary cart-line verification is implemented. The authentication
redirect is a technical source-access blocker, not a request to repeat the
user's cart-verification authorization.
