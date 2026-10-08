"""The judgments, defined once and asked of every backend in the same words.

Jev takes these verbatim (TypeSafe System One format). The gemma backend renders
the same instructions and criteria into a prompt and reads the answer from token
probabilities. If the wording changes, it changes for both -- a comparison between
two models asked different questions measures the questions.

Score levels are 0-indexed, as Jev returns them.

V1 (2026-09-23 run 20260923T082831) framed interest for a crypto outlet only;
the beat is crypto, AI and finance, and Jev -- following the rubric literally --
scored OpenAI, Anthropic and xAI litigation near 0. V1's "buried detail" noul was
answered yes ~0.98 of the time by gemma and 0.83 by Jev: a named person in any
filing satisfied it. Every run directory snapshots the questions it was asked.
"""

QUESTIONS_V1 = {
    "substantive": {
        "type": "noul",
        "instructions": (
            "Is this item a substantive development: a new lawsuit or case, criminal charges, "
            "a court ruling or order that decides something, a settlement or plea, a sentencing, "
            "an enforcement or regulatory action, or newly disclosed facts? Or is it a routine "
            "procedural step?"),
        "criteria": {
            "true": "A substantive development: it changes what is known or what happens next.",
            "false": ("Procedural or administrative: appearances, scheduling, extensions, service, "
                      "mailing, pro hac vice, case assignment, clerical entries, or content that is "
                      "not about a development at all."),
        },
    },
    "subject_is_party": {
        "type": "noul",
        "instructions": (
            "Is the entity or topic named in `watch_term` a party to this matter or its main "
            "subject, rather than merely cited, quoted as precedent, or mentioned in passing?"),
        "criteria": {
            "true": "`watch_term` is a party, a target, or the central subject.",
            "false": ("`watch_term` appears only incidentally, for example as a cited case such as "
                      "'Bielski v. Coinbase' inside an unrelated lawsuit."),
        },
    },
    "event": {
        "type": "choice",
        "instructions": "What kind of event does this item record?",
        "criteria": {
            "new_case": "A new lawsuit, complaint, petition or bankruptcy filing.",
            "charges": "Criminal charges: an indictment, information, criminal complaint or arrest.",
            "ruling": "A court order or opinion that decides a contested question.",
            "settlement_or_plea": "A settlement, consent judgment or plea agreement.",
            "sentencing": "A sentencing, or a judgment imposing a penalty.",
            "enforcement_action": "An agency enforcement action, penalty, sanction or seizure.",
            "regulatory_statement": ("A statement, rule, guidance or announcement by a regulator, "
                                     "official or government."),
            "new_disclosure": ("A filing or release that discloses new facts: exhibits, "
                               "declarations, testimony or data."),
            "procedural": "A routine procedural or administrative step.",
            "unrelated": ("None of the above, or not about crypto, finance or technology "
                          "companies at all."),
        },
    },
    "protos_interest": {
        "type": "score",
        "instructions": (
            "How much would a reader of an investigative crypto news outlet (one that covers crypto "
            "companies and their executives, stablecoins, fraud, enforcement, and the politics of "
            "crypto) want to be told about this item?"),
        "criteria": [
            "Not at all: unrelated to crypto or its major players, or only incidentally connected.",
            "Barely: a routine step in a case or matter such a reader might already follow.",
            "Somewhat: a minor development in a known crypto story.",
            ("Clearly: a new party, dollar amount, ruling or document that a crypto reader would "
             "want to know about."),
            ("Must know: new charges, a new case, or an enforcement action against a crypto company "
             "or figure, or a document exposing something not previously public."),
        ],
    },
    "buried_detail": {
        "type": "noul",
        "instructions": (
            "Does this item contain a specific detail (a named individual, a dollar amount, a "
            "wallet or account identifier, or a corporate relationship) that a general-news summary "
            "of it would probably leave out?"),
        "criteria": {
            "true": "At least one specific, reportable detail of that kind is in the text.",
            "false": "No such detail, or the text is too thin to tell.",
        },
    },
}


BEAT = ("an investigative news outlet that covers crypto, AI companies and finance: the companies "
        "and their executives, fraud, enforcement, litigation, regulation, and the politics around them")

QUESTIONS = {k: v for k, v in QUESTIONS_V1.items() if k not in ("protos_interest", "buried_detail")}
QUESTIONS["protos_interest"] = {
    "type": "score",
    "instructions": f"How much would a reader of {BEAT} want to be told about this item?",
    "criteria": [
        "Not at all: unrelated to crypto, AI or finance, or only incidentally connected.",
        "Barely: a routine step in a case or matter such a reader might already follow.",
        "Somewhat: a minor development in a known crypto, AI or finance story.",
        ("Clearly: a new party, dollar amount, ruling or document that a reader following crypto, AI "
         "or finance would want to know about."),
        ("Must know: new charges, a new case, or an enforcement action against a crypto, AI or "
         "financial company or a prominent figure in one, or a document exposing something not "
         "previously public."),
    ],
}
QUESTIONS["buried_detail"] = {
    "type": "noul",
    "instructions": (
        "Would someone who read only this item's headline or case caption miss its most newsworthy "
        "fact? For example, a dollar amount, an asset seized, a named executive or intermediary, or a "
        "link between companies that appears only in the filing text."),
    "criteria": {
        "true": "The body carries a newsworthy specific that the headline or caption does not reveal.",
        "false": ("The headline or caption already says what matters, or the item is routine or too thin "
                  "to contain such a fact."),
    },
}
