"""The judgments, defined once and asked of every backend in the same words.

Jev takes these verbatim (TypeSafe System One format). The gemma backend renders
the same instructions and criteria into a prompt and reads the answer from token
probabilities. If the wording changes, it changes for both -- a comparison between
two models asked different questions measures the questions.

Score levels are 0-indexed, as Jev returns them.
"""

QUESTIONS = {
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
