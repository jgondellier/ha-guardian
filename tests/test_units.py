"""Fonctions sans effet de bord : analyse, signatures, réglages, versions, sauvegardes."""
import time
import urllib.parse

import pytest

from app import backups, db, devices, notify, updater

SYS_OUTPUT = (
    '{"result":"ok","data":{"disk_total":58.0,"disk_used":20.3,"disk_free":37.7}}\n@@\n'
    "MemTotal: 8000000 kB\nMemFree: 100 kB\nMemAvailable: 6000000 kB\n@@\n"
    "0.42 0.30 0.20 1/200 123\n@@\n45000\n52000\n@@\n4\n@@\n123456.7 999\n"
)


def test_analyse_sante_mini_pc():
    s = devices.parse_system(SYS_OUTPUT)
    assert s["disk_percent"] == 35.0 and s["disk_free_gb"] == 37.7
    assert s["mem_percent"] == 25.0 and s["temp_c"] == 52.0
    assert s["cpus"] == 4 and s["uptime_s"] == 123456 and s["load1"] == 0.42


def test_analyse_sortie_vide_ou_erreur():
    assert devices.parse_system("zsh: no matches found\n@@\n@@\n@@\n@@\n@@") == {}
    assert devices.parse_system("") == {}


def test_commande_ha_dans_un_shell_de_connexion():
    # Sans shell de connexion, l'add-on SSH ne charge pas SUPERVISOR_TOKEN.
    assert devices.ha_cli("core restart") == "sh -lc 'ha core restart'"
    cmd = devices.ha_cli('backups new --name "HA Guardian 2026-11-22 22:30" --raw-json')
    assert cmd == """sh -lc 'ha backups new --name "HA Guardian 2026-11-22 22:30" --raw-json'"""
    assert devices._SYS_CMD.startswith("sh -lc ")


def test_lien_de_notification_signe():
    url = notify.hook_url("plug_cycle")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    exp, sig = int(q["exp"]), q["sig"]
    assert url.startswith("http://guardian.test/api/hook/plug_cycle")
    assert notify.verify_hook("plug_cycle", exp, sig)
    assert not notify.verify_hook("plug_off", exp, sig)          # action non autorisée
    assert not notify.verify_hook("wol", exp, sig)                # signature d'une autre action
    assert not notify.verify_hook("plug_cycle", exp + 1, sig)     # date modifiée
    assert not notify.verify_hook("plug_cycle", int(time.time()) - 1,
                                  notify._hook_sig("plug_cycle", int(time.time()) - 1))


def test_reglages_types_et_bornes(env):
    s = db.save_settings({"backup_enabled": "on", "slow_ms": "1500.7", "fail_core_restart": -4,
                          "sentinel_entities": 123, "inconnu": 1})
    assert s["backup_enabled"] is True and s["slow_ms"] == 1500
    assert s["fail_core_restart"] == 0 and s["sentinel_entities"] == "123"
    assert "inconnu" not in s


@pytest.mark.parametrize("candidate,current,expected", [
    ("v2.1.0", "2.0.0", True), ("2.0.10", "2.0.9", True), ("v2.0.0", "2.0.0", False),
    ("1.9.9", "2.0.0", False), ("v3.0.0", "dev", False), ("nimporte", "2.0.0", False),
])
def test_comparaison_de_versions(candidate, current, expected):
    assert updater.is_newer(candidate, current) is expected


def test_choix_du_fichier_de_sauvegarde():
    now = time.time()
    files = [
        {"name": "vieux.tar", "mtime": now - 86400, "size": 1},
        {"name": "ab12cd34.tar", "mtime": now - 50, "size": 2},
        {"name": "HA_Guardian_2026.tar", "mtime": now - 10, "size": 3},
        {"name": "notes.txt", "mtime": now, "size": 4},
    ]
    assert backups.pick_remote_file(files, "ab12cd34", now - 60)["name"] == "ab12cd34.tar"
    assert backups.pick_remote_file(files, None, now - 60)["name"] == "HA_Guardian_2026.tar"
    assert backups.pick_remote_file(files, "inconnu", now - 60)["name"] == "HA_Guardian_2026.tar"
    assert backups.pick_remote_file(files[:1], None, now - 60) is None


def test_telechargement_refuse_les_chemins_douteux():
    for name in ("../secret.key", "a/b.tar", "x.tar/../../y", ".tar", "data.db"):
        assert backups.local_path(name) is None
