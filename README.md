# Sanket feed
Reads NEPSE market data with the open-source NepseUnofficialApi library and posts it to the Sanket API.
The API address and key are GitHub secrets (`NP_API`, `NP_KEY`); nothing private is in this repository.
- `feed.py auto`: live prices during the session, then the daily close.
- `feed.py history`: older daily prices, a little each day within the free database budget.
