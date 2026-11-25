"""Local reference data and its pinned installation inventory.

You can put the two packaged .json.gz files in COMPANY_OS_DATA_DIR/knowledge.
Otherwise they load from the repository or installed package. Source workbooks
and extraction files are optional and are only read by the integrity check.
Run SUPYMEM_MODE=local python -m app.knowledge.local_data to verify your files.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

from app.core.local_mode import is_local

BUNDLED_FILES = {
    "data/knowledge/startup-pmf-2026-09-19/README.md": (3451, "4dc92aad9997f8b9bb1bbd0583f958ef249be3fc664f01e8b9e596f2581f0682"),
    "data/knowledge/startup-pmf-2026-09-19/audit.json": (3194, "2ee1b3fd149d020a94be25ac22d2886298af2274f97cf76b1ac32716aa4ac82c"),
    "data/knowledge/startup-pmf-2026-09-19/cells.jsonl": (13542252, "dbccd68143a5c39f73b2ff73af18c03835fde2851cb26f8d81b8defd8319f48e"),
    "data/knowledge/startup-pmf-2026-09-19/manifest.json": (18292, "08acd9cd349e0cd5117083f1c291eccffeaae0c7bcecf142d9b065f04637515a"),
    "data/knowledge/startup-pmf-2026-09-19/original.xlsx": (689254, "098b65c2c5ccca67af841f9b6756add3a64dfb705054ae342495cd6befe7efaa"),
    "data/knowledge/startup-pmf-2026-09-19/records.jsonl": (14084254, "46a20473246a468fe564b94b498594b13b6efe8e29dfe4c8f64f74363fdaf529"),
    "data/knowledge/startup-pmf-2026-09-19/references.jsonl": (929233, "fff2c635104fbfa4c9f0e3c2000669caead3c9652079470202e31f108f0ee677"),
    "data/knowledge/startup-pmf-2026-09-19/sources.json": (220918, "33b2637e92b6ca25c392be9fefc141ab07e178b0e38310531358667fb84fa83f"),
    "data/knowledge/yc-video-advice-1dfdaf90/README.md": (2452, "033236859a084ea69102ef1078e8816ebdde597736d9220d4d308a9f7503d47c"),
    "data/knowledge/yc-video-advice-1dfdaf90/UPSTREAM_PIN.json": (951, "d288d34e4abb53b694d0313b668a8954d239221c14c5f5407f468702bf967a8b"),
    "data/knowledge/yc-video-advice-1dfdaf90/manifest.json": (1055, "b169bba3569a162918c513c1281acc38d9721801c273679f602c160aadc69579"),
    "services/company_os/app/knowledge/startup_pmf.json.gz": (1038830, "4cc6a18da3dc7d020255bbf1163b2fe30fb7abc9cad5aaba8319783a0b54b4a8"),
    "services/company_os/app/knowledge/yc_video_advice.json.gz": (637831, "cded32b339200c537fdb1bed405ab6017cb2b49aa3f30334b25189eecd5d12ac"),
}


def _knowledge_dir() -> Path:
    from app.config import data_root

    return data_root() / "knowledge"


def _runtime_path(packaged: Path) -> Path:
    candidate = _knowledge_dir() / packaged.name
    return candidate if candidate.exists() else packaged


class ReferenceDataError(ValueError):
    pass


def availability_message(*datasets: dict) -> dict:
    names = list(dict.fromkeys(data["missing_pack"] for data in datasets if data.get("missing_pack")))
    if not names:
        return {}
    return {"message": f"Your founder coaching files are missing. Copy {', '.join(names)} from your app's bundled files into {_knowledge_dir()} and restart your app."}


def _restore_message(name: str) -> str:
    return f"Your founder coaching file could not be verified. Restore {name} at {_knowledge_dir() / name} from your app's bundled files and restart your app."


def load_corpus(packaged: Path) -> dict | None:
    if not is_local():
        return None
    try:
        path = _runtime_path(packaged)
        raw = path.read_bytes()
    except FileNotFoundError:
        data = {"dataset_id": None, "sha256": None, "corpus_sha256": None, "missing_pack": packaged.name}
        return {**data, **availability_message(data)}
    except OSError as error:
        raise OSError(f"Your founder coaching file {packaged.name} could not be read. Copy a readable bundled version to {_knowledge_dir() / packaged.name} and restart your app.") from error
    digest = hashlib.sha256(raw).hexdigest()
    expected = BUNDLED_FILES[f"services/company_os/app/knowledge/{packaged.name}"]
    if (len(raw), digest) != expected:
        raise ReferenceDataError(_restore_message(packaged.name))
    data = json.loads(gzip.decompress(raw))
    data["corpus_sha256"] = digest
    return data


def verify_data() -> dict:
    if not is_local():
        return {"enabled": False, "ok": True, "files": []}
    files = []
    for name, (size, digest) in BUNDLED_FILES.items():
        relative = Path(name)
        entry = {"path": name, "size_bytes": size, "sha256": digest, "optional": not name.startswith("services/")}
        try:
            path = (_runtime_path(Path(__file__).with_name(relative.name))
                    if name.startswith("services/") else _knowledge_dir() / relative.relative_to("data/knowledge"))
            with path.open("rb") as stream:
                actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
                actual_size = os.fstat(stream.fileno()).st_size
        except FileNotFoundError:
            entry.update(status="missing", **(availability_message({"missing_pack": relative.name}) if not entry["optional"] else
                         {"message": f"Your optional reference data {relative.name} is missing. You can copy it from the bundled data to enable it."}))
        except OSError:
            entry.update(status="unreadable", message=f"Your reference data {relative.name} could not be read. Check the file and its permissions, then try again.")
        else:
            entry["status"] = "verified" if (actual_size, actual_digest) == (size, digest) else "mismatch"
            if entry["status"] == "mismatch":
                entry["message"] = _restore_message(relative.relative_to("data/knowledge").as_posix() if entry["optional"] else relative.name)
        files.append(entry)
    return {"enabled": True, "ok": all(entry["status"] == "verified" or entry["status"] == "missing" and entry["optional"] for entry in files), "files": files}


if __name__ == "__main__":
    report = verify_data()
    if not report["enabled"]:
        report["message"] = "Set SUPYMEM_MODE=local and run this check again to verify your bundled reference data."
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
