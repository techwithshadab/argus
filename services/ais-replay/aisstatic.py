"""AIS static data (message 5, AISStream `ShipStaticData`) into the vessel and registry tables.
Pure: no I/O, unit-tested. The flag comes from the MMSI's maritime identification digits; the
ship type from the AIS type code."""

from __future__ import annotations

from datetime import UTC, datetime

# Maritime Identification Digits for the flags that dominate merchant traffic; unknown MIDs
# yield no flag rather than a guess.
MID_FLAGS = {
    "201": "AL",
    "203": "AT",
    "205": "BE",
    "207": "BG",
    "209": "CY",
    "210": "CY",
    "211": "DE",
    "212": "CY",
    "215": "MT",
    "218": "DE",
    "219": "DK",
    "220": "DK",
    "224": "ES",
    "225": "ES",
    "226": "FR",
    "227": "FR",
    "228": "FR",
    "229": "MT",
    "230": "FI",
    "231": "FO",
    "232": "GB",
    "233": "GB",
    "234": "GB",
    "235": "GB",
    "236": "GI",
    "237": "GR",
    "238": "HR",
    "239": "GR",
    "240": "GR",
    "241": "GR",
    "242": "MA",
    "244": "NL",
    "245": "NL",
    "246": "NL",
    "247": "IT",
    "248": "MT",
    "249": "MT",
    "250": "IE",
    "251": "IS",
    "252": "LI",
    "253": "LU",
    "255": "PT",
    "256": "MT",
    "257": "NO",
    "258": "NO",
    "259": "NO",
    "261": "PL",
    "263": "PT",
    "264": "RO",
    "265": "SE",
    "266": "SE",
    "267": "SK",
    "268": "SM",
    "269": "CH",
    "271": "TR",
    "272": "UA",
    "273": "RU",
    "274": "MK",
    "275": "LV",
    "276": "EE",
    "277": "LT",
    "278": "SI",
    "279": "RS",
    "301": "AI",
    "303": "US",
    "304": "AG",
    "305": "AG",
    "306": "CW",
    "307": "AW",
    "308": "BS",
    "309": "BS",
    "310": "BM",
    "311": "BS",
    "312": "BZ",
    "314": "BB",
    "316": "CA",
    "319": "KY",
    "321": "CR",
    "325": "DM",
    "327": "DO",
    "329": "GP",
    "330": "GD",
    "338": "US",
    "339": "JM",
    "341": "KN",
    "345": "MX",
    "351": "PA",
    "352": "PA",
    "353": "PA",
    "354": "PA",
    "355": "PA",
    "356": "PA",
    "357": "PA",
    "358": "PR",
    "359": "SV",
    "366": "US",
    "367": "US",
    "368": "US",
    "369": "US",
    "370": "PA",
    "371": "PA",
    "372": "PA",
    "373": "PA",
    "374": "PA",
    "375": "VC",
    "376": "VC",
    "377": "VC",
    "378": "VG",
    "403": "SA",
    "405": "BD",
    "412": "CN",
    "413": "CN",
    "414": "CN",
    "416": "TW",
    "419": "IN",
    "422": "IR",
    "423": "AZ",
    "425": "IQ",
    "428": "IL",
    "431": "JP",
    "432": "JP",
    "436": "KZ",
    "437": "KW",
    "440": "KR",
    "441": "KR",
    "443": "PS",
    "445": "KP",
    "447": "OM",
    "450": "LB",
    "453": "MO",
    "457": "MN",
    "461": "QA",
    "463": "PK",
    "466": "AE",
    "470": "AE",
    "471": "AE",
    "472": "TJ",
    "473": "YE",
    "477": "HK",
    "511": "PW",
    "512": "NZ",
    "514": "KH",
    "515": "KH",
    "516": "CX",
    "518": "CK",
    "525": "ID",
    "529": "KI",
    "533": "MY",
    "536": "MP",
    "538": "MH",
    "540": "NC",
    "548": "PH",
    "553": "PG",
    "555": "PN",
    "563": "SG",
    "564": "SG",
    "565": "SG",
    "566": "SG",
    "567": "TH",
    "570": "TO",
    "574": "VN",
    "576": "VU",
    "577": "VU",
    "601": "ZA",
    "603": "AO",
    "605": "DZ",
    "607": "TF",
    "608": "SH",
    "612": "CM",
    "613": "CM",
    "615": "CG",
    "616": "KM",
    "619": "CI",
    "621": "DJ",
    "622": "EG",
    "624": "ET",
    "626": "GA",
    "627": "GH",
    "630": "GW",
    "631": "GQ",
    "632": "GN",
    "636": "LR",
    "637": "LR",
    "642": "LY",
    "644": "MG",
    "649": "MU",
    "654": "MR",
    "655": "MW",
    "656": "NE",
    "657": "NG",
    "659": "NA",
    "660": "RE",
    "664": "SC",
    "665": "SH",
    "666": "SO",
    "667": "SL",
    "668": "SD",
    "669": "SN",
    "670": "TZ",
    "671": "TG",
    "672": "TN",
    "674": "TZ",
    "675": "UG",
    "676": "CD",
    "677": "TZ",
    "678": "ZM",
    "679": "ZW",
    "701": "AR",
    "710": "BR",
    "720": "BO",
    "725": "CL",
    "730": "CO",
    "735": "EC",
    "740": "FK",
    "745": "GF",
    "750": "GY",
    "755": "PY",
    "760": "PE",
    "765": "SR",
    "770": "UY",
    "775": "VE",
}


def flag_from_mmsi(mmsi: int | str | None) -> str | None:
    s = str(mmsi or "")
    return MID_FLAGS.get(s[:3]) if len(s) == 9 else None


def ship_type_text(code: int | None) -> str | None:
    """AIS ship-type code to the categories the detectors and prompts use."""
    if code is None:
        return None
    c = int(code)
    if 70 <= c <= 79:
        return "cargo"
    if 80 <= c <= 89:
        return "tanker"
    if 60 <= c <= 69:
        return "passenger"
    if 40 <= c <= 49:
        return "high_speed_craft"
    if 20 <= c <= 29:
        return "wing_in_ground"
    if 90 <= c <= 99:
        return "other"
    return {
        30: "fishing",
        31: "towing",
        32: "towing",
        33: "dredging",
        34: "diving",
        35: "military",
        36: "sailing",
        37: "pleasure",
        50: "pilot",
        51: "search_and_rescue",
        52: "tug",
        53: "port_tender",
        54: "anti_pollution",
        55: "law_enforcement",
        58: "medical",
        59: "non_combatant",
    }.get(c)


def static_rows(msg: dict) -> tuple[dict | None, dict | None]:
    """(vessel row, registry row) from an AISStream ShipStaticData message, or (None, None)."""
    data = (msg.get("Message") or {}).get("ShipStaticData") or {}
    mmsi = data.get("UserID")
    if not mmsi:
        return None, None
    dim = data.get("Dimension") or {}
    length = (dim.get("A") or 0) + (dim.get("B") or 0)
    name = (
        (msg.get("MetaData") or {}).get("ShipName") or data.get("Name") or ""
    ).strip()
    imo = data.get("ImoNumber") or None
    vessel = {
        "mmsi": int(mmsi),
        "imo": int(imo) if imo else None,
        "name": name or None,
        "callsign": (data.get("CallSign") or "").strip() or None,
        "flag": flag_from_mmsi(mmsi),
        "ship_type": ship_type_text(data.get("Type")),
        "length_m": float(length) if length else None,
        "meta": {
            "destination": (data.get("Destination") or "").strip() or None,
            "draught_m": data.get("MaximumStaticDraught"),
            "eta": data.get("Eta"),
            "source": "aisstream",
            "seen_at": datetime.now(UTC).isoformat(),
        },
    }
    registry = {
        "mmsi": vessel["mmsi"],
        "imo": vessel["imo"],
        "name": vessel["name"],
        "flag": vessel["flag"],
        "notes": "Identity from AIS static data (AISStream); ownership not available from this feed.",
    }
    return vessel, registry


def upsert_vessel_sql(v: dict) -> tuple[str, tuple]:
    """Insert or refresh a live vessel; never touch rows the scenario loaded (is_synthetic)."""
    return (
        """INSERT INTO vessels (mmsi, imo, name, callsign, flag, ship_type, length_m, is_synthetic, meta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s::jsonb)
           ON CONFLICT (mmsi) DO UPDATE SET
             imo = COALESCE(EXCLUDED.imo, vessels.imo), name = COALESCE(EXCLUDED.name, vessels.name),
             callsign = COALESCE(EXCLUDED.callsign, vessels.callsign), flag = COALESCE(EXCLUDED.flag, vessels.flag),
             ship_type = COALESCE(EXCLUDED.ship_type, vessels.ship_type),
             length_m = COALESCE(EXCLUDED.length_m, vessels.length_m), meta = vessels.meta || EXCLUDED.meta
           WHERE vessels.is_synthetic = false""",
        (
            v["mmsi"],
            v["imo"],
            v["name"],
            v["callsign"],
            v["flag"],
            v["ship_type"],
            v["length_m"],
            __import__("json").dumps(v["meta"]),
        ),
    )


def upsert_registry_sql(r: dict) -> tuple[str, tuple]:
    """A registry row so identity lookups answer; an existing (scenario or curated) row wins."""
    return (
        """INSERT INTO registry (mmsi, imo, name, flag, notes)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (mmsi) DO NOTHING""",
        (r["mmsi"], r["imo"], r["name"], r["flag"], r["notes"]),
    )
