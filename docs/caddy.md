# Caddy Reverse Proxy

These snippets are examples only. They are meant to be adapted into an operator-managed Caddy setup, not treated as a one-size-fits-all generated config.

The application itself should stay on:

`127.0.0.1:48231`

## Domain Example

```caddy
your-domain.example {
    reverse_proxy 127.0.0.1:48231
}
```

Caddy can automatically manage Let's Encrypt when the domain points to the VPS.

## IP-Only Example

```caddy
http://SERVER_IP {
    reverse_proxy 127.0.0.1:48231
}
```

That is the simplest domainless option.

For `https://SERVER_IP`, use a manual certificate block and expect more setup friction than the domain case.
