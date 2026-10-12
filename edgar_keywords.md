# EDGAR company-name keywords (newshound edgar_feed.py reads this every run), for filers not on a watch list.
# Matched on every party's name (Issuer, Reporting, Filer, Subject), case-insensitive, as whole words:
# "Bitcoin Depot" matches bitcoin; "Tronox" does not match tron, nor "Cryptologic" crypto.
#   term                       any form
#   term | forms: 8-K S-1 424B*   only those forms (an amendment /A counts; "*" ends a prefix; "SC 13D" is one form)
#   treasury + w1 w2 ...       "treasury" and any one of the words in the same name
# A matching 8-K, 6-K, 13D/G, registration statement, 424B*, merger proxy, delisting or late notice pings on its
# own; any other form (10-Q, Form 4, 13F, Form D ...) goes into the run's one "routine" batch post.
bitcoin
btc
crypto
cryptocurrency
cryptocurrencies
digital asset
digital assets
stablecoin
stablecoins
blockchain
blockchains
ethereum
ether
solana
tron
xrp
mining | forms: 8-K S-1 S-4 424B* SC 13D* SC 13G* SCHEDULE 13D* SCHEDULE 13G*
# the crypto words above already match alone; these catch treasury companies named for other tokens
treasury + eth sol doge bnb avax sui ton tao hype ltc token tokens coin digital web3 defi
