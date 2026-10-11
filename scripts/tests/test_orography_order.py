#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test ORDINE DELLE SOGLIE OROGRAFICHE (L2) — common.OROGRAPHY_CLASS.

La costruzione è last-wins nell'ordine H -> M2 -> L (port fedele dell'app
buildProvinceSamplesV2). I duplicati sono VOLUTI, NON riordinare: ogni
modifica all'ordine o al contenuto degli elenchi rompe questi pin."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import OROGRAPHY_CLASS  # noqa: E402


def test_duplicate_nu_resolves_to_m2():
    # NU è in _ORO_H e _ORO_M2 -> prevale M2 (Sardegna, pianeggiante)
    assert OROGRAPHY_CLASS["NU"] == "M2"


def test_duplicate_le_resolves_to_l():
    # LE è in _ORO_M2 e _ORO_L -> prevale L (Lecce)
    assert OROGRAPHY_CLASS["LE"] == "L"


def test_further_duplicates_consistent_with_last_wins():
    # M2/L duplicati -> L (l'ultimo elenco vince)
    for code in ("MI", "MB", "LO", "RA", "FC", "RN", "RM", "LT"):
        assert OROGRAPHY_CLASS[code] == "L"


def test_unique_provinces_resolvable():
    assert OROGRAPHY_CLASS["AO"] == "H"
    assert OROGRAPHY_CLASS["SS"] == "H"
    assert OROGRAPHY_CLASS["OG"] == "H"
    assert OROGRAPHY_CLASS["NA"] == "L"
    assert OROGRAPHY_CLASS["GE"] == "H"


def test_all_provinces_mapped():
    expected = len(
        set("AO BZ TN SO BL CN VC VB UD PN BG BS PC PR GE IM SV AQ MC AP FM "
            "PG RI IS PZ CS CZ VV KR EN NU SS OT OR OG".split())
        | set("CO LC MB TO AT AL NO BI VA FC RN RA TR PG AR SI GR LI PI PT PO "
              "FI LU MS RM VT FR LT BN AV SA CB FG BA BT TA BR LE RC ME CT SR "
              "RG CL AG TP PA CA CI VS LE NU".split())
        | set("MI MB LO PV CR MN VE PD TV VR RO FE BO MO RE RA FC RN PU AN TE "
              "PE CH LE LT NA CE RM".split()))
    # gli ultimi due elenchi (e anche i precedenti) coprono molti codici;
    # il point è che OGNI codice presente negli elenchi è stato mappato.
    assert len(OROGRAPHY_CLASS) >= 100
    assert len(OROGRAPHY_CLASS) == expected