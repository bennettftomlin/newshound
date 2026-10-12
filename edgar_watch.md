# EDGAR watched CIKs beyond the knowledge base (newshound edgar_feed.py reads this every run).
# One "<CIK> <label>" per line; '#' starts a comment. Every CIK the knowledge base types (derived/graph.sqlite
# listings, kind cik or filer, from note frontmatter) is watched already; list here holders and people the KB has
# not typed yet, or ones that must stay watched if the graph cannot be read. A watched CIK matches as Issuer,
# Reporting owner, Filer or Subject. Use the CIK, never a name: issuers rename.
0002025889 Tether (holder; filer CIK, KB Tether.md)
0001065521 SoftBank Group (holder; filer CIK, KB SoftBank.md)
