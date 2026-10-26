# Security policy

## Reporting a vulnerability

Please do not report security problems in public issues. Use GitHub's private vulnerability reporting (**Security → Report a vulnerability** on this repository) or contact the maintainer privately through the GitHub profile [@artepe](https://github.com/artepe). Please allow time for a fix before publishing details.

## Supported versions

Security fixes are made to the latest release and the `main` branch only.

## Security design

- The controller runs inside Home Assistant and acts only through Home Assistant services on the configured Mango Tuya Local entities; it opens no network connections and stores no credentials.
- The end-to-end test helper fetches Mango Tuya Local at a pinned commit.
