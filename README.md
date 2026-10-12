# GroknMok

GroknMok helps hobby gamers find Games Workshop products in stock at
independent stores in Canada and the United States. It is a private,
independent project, not affiliated with Games Workshop or any store, and
it is not available for download here.

## Store owners

GroknMok's crawler identifies itself as `40k-stockcheck`, and its live
checks as `groknmok`. [STORES.md](STORES.md) explains what it collects from your public
online catalogue, how often, and how to have your store left out:

- contact groknmoksupport@gmail.com, or open a
  ["Remove my store" issue](https://github.com/JustinCreates/GroknMok/issues/new?template=remove-my-store.yml);
- or block the crawler in your robots.txt:

  ```
  User-agent: 40k-stockcheck
  Disallow: /
  ```

Your store is removed within 3 business days of a valid request.

Games Workshop, Warhammer, Citadel and Forge World are trademarks of
Games Workshop Limited.
