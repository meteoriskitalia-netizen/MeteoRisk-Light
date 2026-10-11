#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test risoluzione SAMPLES_ROOT (L1) — validation.cases.samples_root.

Priorità: env METEORISK_SAMPLES_DIR -> env RADAR_SAMPLES_DIR (legacy) -> path
relativo alla repo (scripts/_samples) se esiste -> percorso assoluto legacy.
"""

import os

from radar_engine.validation import cases


def test_meteorisk_env_wins(monkeypatch):
    monkeypatch.setenv("METEORISK_SAMPLES_DIR", r"C:\meteorisk_samples")
    monkeypatch.setenv("RADAR_SAMPLES_DIR", r"C:\radar_samples")
    monkeypatch.setattr(cases, "_REPO_RELATIVE_SAMPLES", r"C:\nonexistent_rel")
    assert cases.samples_root() == r"C:\meteorisk_samples"


def test_legacy_env_fallback(monkeypatch):
    monkeypatch.delenv("METEORISK_SAMPLES_DIR", raising=False)
    monkeypatch.setenv("RADAR_SAMPLES_DIR", r"C:\radar_samples")
    monkeypatch.setattr(cases, "_REPO_RELATIVE_SAMPLES", r"C:\nonexistent_rel")
    assert cases.samples_root() == r"C:\radar_samples"


def test_repo_relative_fallback_when_no_env(monkeypatch):
    monkeypatch.delenv("METEORISK_SAMPLES_DIR", raising=False)
    monkeypatch.delenv("RADAR_SAMPLES_DIR", raising=False)
    monkeypatch.setattr(cases, "_REPO_RELATIVE_SAMPLES", os.getcwd())  # esiste
    assert cases.samples_root() == os.getcwd()


def test_legacy_absolute_is_last_resort(monkeypatch):
    monkeypatch.delenv("METEORISK_SAMPLES_DIR", raising=False)
    monkeypatch.delenv("RADAR_SAMPLES_DIR", raising=False)
    monkeypatch.setattr(cases, "_REPO_RELATIVE_SAMPLES", r"C:\nonexistent_rel")
    assert cases.samples_root() == cases.DEFAULT_SAMPLES_DIR


def test_explicit_env_var_arg_respected_with_priority(monkeypatch):
    monkeypatch.setenv("METEORISK_SAMPLES_DIR", r"C:\meteorisk_samples")
    monkeypatch.setenv("RADAR_SAMPLES_DIR", r"C:\radar_samples")
    assert cases.samples_root(env_var="CUSTOM_SAMPLES_DIR") == r"C:\meteorisk_samples"
    monkeypatch.delenv("METEORISK_SAMPLES_DIR")
    assert cases.samples_root(env_var="CUSTOM_SAMPLES_DIR") == r"C:\radar_samples"
    monkeypatch.delenv("RADAR_SAMPLES_DIR")
    monkeypatch.setenv("CUSTOM_SAMPLES_DIR", r"C:\custom_samples")
    assert cases.samples_root(env_var="CUSTOM_SAMPLES_DIR") == r"C:\custom_samples"