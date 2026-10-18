# Audi FC Bayern Ticket Finder

`ticketfinder` is a local command-line monitor for matching FC Bayern tickets
listed in the Audi Regiondo ticket shop. It scans the category pages in order
and matches every comma-separated keyword against the same product title,
without regard to letter case.

When the first matching product is sold out, Ticketfinder sends one sold-out
email and keeps checking that product at the configured interval. When it
becomes available, the program either sends an availability email or, with
`-c`, adds up to two tickets and sends a cart email. It never submits an order
or makes a payment.

> **Eligibility:** Ticket purchases may be restricted by the seller. Check the
> ticket's terms and use Ticketfinder only if your account is eligible. The
> script limits an add-to-cart action to two tickets.

## Requirements

- Python 3.10 or newer
- Google Chrome on macOS if Playwright's bundled Chromium is unavailable
- An eligible Regiondo account if the ticket page requires login
- SMTP access for the email account used for notifications

## Installation

From the project directory, create a virtual environment, install the
dependencies, and register the `ticketfinder` terminal command:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
cp .env.example .env
```

Ticketfinder first tries Playwright's bundled Chromium and then tries the
Google Chrome application installed at `/Applications/Google Chrome.app`. If
neither browser is available, install Google Chrome and run the command again.

## Configuration

Edit `.env` locally. Do not upload it or share its contents. The repository's
`.gitignore` excludes `.env`.

Set the SMTP host, port, sender, and recipient. For Gmail, the usual settings
are `smtp.gmail.com` and port `587` (STARTTLS). This program uses SMTP
username/password authentication; for a personal Google account, use a Google
App Password when required, not your normal account password. Store the App
Password only in the local `.env` file.

`SITE_USERNAME` and `SITE_PASSWORD` are optional and are used only if the site
shows a login form. Keep `HEADLESS=false` to see the browser. The browser
session and its cart are isolated from your everyday Chrome profile; when using
`-c`, continue checkout in the Ticketfinder browser window.

## Usage

Email-only mode (the default) sends an email when the matching ticket becomes
available and then exits. It does not add anything to a cart:

```bash
ticketfinder -k "FC MAGDEBURG, Ingolstadt"
```

To add up to two tickets to the Ticketfinder browser session, display the cart
URL in the terminal, and send the success email, include `-c`:

```bash
ticketfinder -c -k "FC MAGDEBURG, Ingolstadt"
```

Schedule a start time and set the polling interval in seconds:

```bash
ticketfinder -c -d 18.10.2026 -t 23:15 -i 60 -k "FC MAGDEBURG, Ingolstadt"
```

| Option | Meaning | Default |
| --- | --- | --- |
| `-d DD.MM.YYYY` | Start date in `Europe/Berlin`; use together with `-t` | Start immediately |
| `-t HH:MM` | Start time in `Europe/Berlin`; use together with `-d` | Start immediately |
| `-i SECONDS` | Delay between checks while waiting for availability | `60` |
| `-k KEYWORDS` | Required, comma-separated title terms; all terms must occur in one title | — |
| `-c` | Add tickets to the cart when available | Email only |

If the supplied start time has already passed, monitoring starts immediately.
Stop the program with `Ctrl+C`.

## Monitoring and notifications

- The first matching product is selected by pagination order and card order,
  then remains the target for the rest of that run.
- If that product shows `AUSVERKAUFT`, a sold-out email is sent once per run.
  Monitoring continues and retries the email if delivery fails.
- Without `-c`, an availability email is retried if SMTP fails; after a
  successful send, the run ends.
- With `-c`, the cheapest ticket option with a visible price is selected and
  the cart is verified before the success email is sent. A mail retry does not
  repeat a confirmed add-to-cart action.
- The terminal displays Chinese status messages with Berlin-local timestamps.
  Logs are also written to `ticketfinder.log`.
- The script does not bypass CAPTCHA, access restrictions, or other
  anti-automation controls.

## Troubleshooting

- **`ticketfinder: command not found`:** activate the virtual environment with
  `source .venv/bin/activate`, or run `.venv/bin/ticketfinder` directly.
- **Browser launch error on macOS:** install or update Google Chrome. The
  program will try Chrome if the bundled Playwright browser cannot start.
- **Gmail authentication error:** verify the SMTP username and App Password in
  `.env`. Do not paste passwords into terminal commands or public issues.
- **No title match:** ensure every `-k` term appears in the product title;
  terms are separated by commas. For example, the city is spelled
  `Ingolstadt`.
- **Cart not visible in your regular Chrome:** carts are tied to the browser
  session. With `-c`, use the cart in the Ticketfinder browser window.
