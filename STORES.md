# For store owners

GroknMok helps hobby gamers find Games Workshop products in stock at
independent stores. It is an independent project, not affiliated
with Games Workshop or any store. This page explains what it collects
about your store, how, and how to have your store left out.

## In short

- **To be left out,** contact groknmoksupport@gmail.com or open a
  ["Remove my store" issue](https://github.com/JustinCreates/GroknMok/issues/new?template=remove-my-store.yml).
  Your store is removed from GroknMok's dataset within 3 business
  days of a valid request.
- **To stop the crawler visiting your site,** add this to your
  robots.txt:

  ```
  User-agent: 40k-stockcheck
  Disallow: /
  ```

  That stops the visits. The crawler re-reads robots.txt about once a
  day, and a store whose robots.txt blocks it is also left out of the
  dataset from then on.

## Which stores are included

Independent stores in Canada and the United States that carry Games
Workshop products and have a website. They were found from public
listings of Warhammer stockists. Stores sharing one
website are treated as one online shop, with their locations listed
beneath it.

## What is collected

Only the Games Workshop part of your public online catalogue. For each
product:

- the title;
- the price and the currency;
- the stock status your site shows: in stock, out of stock, backorder
  or preorder;
- the release date, if the title gives one;
- the barcode or GW catalogue number, if your site exposes one;
- the link to the product page.

## What GroknMok shows

For each store:

- the store's name, city, province or state, and country;
- the website;
- whether it ships;
- the currency of its site;
- the product facts above.

Never shown: street addresses, phone numbers, email addresses,
product descriptions or images.

The dataset may be used only with GroknMok. No licence to redistribute
it is granted. Product facts belong to the stores.

## How your site is visited

**The crawler** identifies itself with a User-Agent starting
`40k-stockcheck/`, and it is built to be light:

- **robots.txt:** it obeys your robots.txt for the token
  `40k-stockcheck`, including Crawl-delay. If your Crawl-delay is over
  60 seconds, the site is not crawled at all.
- **One request at a time:** at least 2 seconds apart, never in
  parallel against the same site. Stores on shared platforms (for
  example square.site or wixsite.com) share one slot per platform.
- **Stops when refused:** it stops at the first HTTP 403 or 429 and
  marks the site blocked. It never tries to get around bot protection.
- **Public catalogue only:** it reads only the public catalogue your
  storefront already offers (for example Shopify's products.json, the
  WooCommerce Store API, Lightspeed's JSON pages, or your sitemap). It
  never logs in, adds to a cart or places orders.
- **Schedule:**
  - the Games Workshop catalogue, once a night;
  - the newest products and preorders, every 3 hours (one or a few
    requests);
  - sitemap-only sites, once a week.

**GroknMok users** can ask the program to re-check a few matched
products on your site before buying (`--live`):

- at most 2 of your products per search;
- with the same robots.txt (token `groknmok`), 2-second gap and stop
  rules;
- identified as `groknmok/<version>`.

## Asking to be left out

Contact groknmoksupport@gmail.com, or open a
["Remove my store" issue](https://github.com/JustinCreates/GroknMok/issues/new?template=remove-my-store.yml)
if you are happy for the request to be public.

Please include:

- **your store's website** and its name and location;
- **which you want:**
  - **Leave out of the dataset:** your store no longer appears in
    GroknMok.
  - **Also stop crawling:** as above, and the crawler stops visiting
    your site.
- **how we can confirm you speak for the store:** for example, write
  from an address at your store's domain.

**What happens next:** your store is removed from GroknMok's dataset
within 3 business days of a valid request. GroknMok users' copies drop it
at their next update.

## Corrections

Prices and stock come from your own site at the time of the snapshot,
and every result shows how old its snapshot is. If something is shown
wrongly, contact groknmoksupport@gmail.com or open an issue.
