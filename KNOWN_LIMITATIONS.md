# Known limitations

- Real-store checkouts cannot be inspected; real purchases are self-reported and labelled as such.
- Most real offers end as a checklist because listings rarely state warranty, returns or tax.
- Seller trust outside eBay is a store-name match; Amazon results do not name the seller.
- Coverage is Google Shopping, Amazon, eBay and Walmart (US), one results page each.
- Product checks (digital items, accessories, used condition) are pattern-based.
- Walmart, non-US markets, PayPal Sandbox, PostgreSQL and Docker are untested live.
- Search, eBay-feedback and image caches are in memory and reset on restart.
- The signing key sits next to the database; there is no key rotation or external timestamping.
