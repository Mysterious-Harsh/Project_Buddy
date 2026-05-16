from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.fs_manage_prompts import FS_MANAGE_TOOL_PROMPT
from buddy.tools.os.fs_utils import err, needs_confirm, ok, resolve_path

TOOL_NAME = "fs_manage"
_TOOL = TOOL_NAME


class FsManage:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: changing filesystem structure — copy, move, delete, mkdir, or rename.\n\n"
                "FUNCTIONS:\n"
                "  manage(action, paths[], destination_dir?, transfers?, confirmed?)\n"
                "    action: 'copy' | 'move' | 'delete' | 'mkdir'\n"
                "    transfers: batch copy/move to multiple destinations [{paths, destination_dir}]\n"
                "  rename(renames[], confirmed?)  — rename file/dir in place; new_name is filename only (no slashes)\n\n"
                "CHAIN: fs_browse find paths → fs_manage. fs_manage is typically a final step.\n"
                "NOT: reading files → fs_read | writing/editing text → fs_write | listing dirs → fs_browse"
            ),
            "prompt": FS_MANAGE_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()
        if fn == "manage":
            return self._manage(arguments)
        if fn == "rename":
            return self._rename(arguments)
        return err(_TOOL, msg=f"Unknown function '{function}'. fs_manage supports: manage, rename.")

    # ── manage ───────────────────────────────────────────────────────────────

    def _expand_globs(self, path_list: List[str], warnings: List[str]) -> List[str]:
        if not any("*" in p or "?" in p or "[" in p for p in path_list):
            return path_list
        expanded: List[str] = []
        for p_str in path_list:
            if "*" in p_str or "?" in p_str or "[" in p_str:
                matches = sorted(str(m) for m in Path(p_str).parent.glob(Path(p_str).name))
                if not matches:
                    warnings.append(f"glob pattern '{p_str}' matched no files")
                else:
                    expanded.extend(matches)
            else:
                expanded.append(p_str)
        return expanded

    def _manage(self, args: Dict[str, Any]) -> Dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        valid = ("copy", "move", "delete", "mkdir")
        if action not in valid:
            return err(_TOOL, action=action, msg=(
                f"manage 'action' must be one of: {', '.join(valid)} — got '{action}'."
            ))

        confirmed = bool(args.get("confirmed", False))
        permanent = bool(args.get("permanent", False))

        # ── batch transfers ───────────────────────────────────────────────────
        transfers_raw = args.get("transfers")
        if transfers_raw and action in ("copy", "move"):
            if not isinstance(transfers_raw, list):
                return err(_TOOL, action=action, msg=(
                    "manage 'transfers' must be a list of {paths, destination_dir} objects."
                ))

            transfers: List[Dict[str, Any]] = []
            for i, t in enumerate(transfers_raw):
                paths_raw = t.get("paths") or []
                dest_raw = str(t.get("destination_dir") or "").strip()
                if not paths_raw or not dest_raw:
                    return err(_TOOL, action=action, msg=(
                        f"transfers[{i}] is missing 'paths' or 'destination_dir' — both are required."
                    ))
                path_list = [resolve_path(str(r).strip()) for r in paths_raw if str(r).strip()]
                glob_warnings: List[str] = []
                path_list = self._expand_globs(path_list, glob_warnings)
                transfers.append({
                    "paths": path_list,
                    "destination_dir": resolve_path(dest_raw),
                    "glob_warnings": glob_warnings,
                })

            if not confirmed:
                all_conflicts: List[str] = []
                for t in transfers:
                    dest_p = Path(t["destination_dir"])
                    if dest_p.is_dir():
                        all_conflicts.extend(
                            str(dest_p / Path(p).name)
                            for p in t["paths"]
                            if (dest_p / Path(p).name).exists()
                        )
                if all_conflicts:
                    preview = (
                        f"manage {action} (batch): {sum(len(t['paths']) for t in transfers)} file(s) "
                        f"to {len(transfers)} destination(s).\n"
                        f"{len(all_conflicts)} destination path(s) already exist and will be overwritten:\n"
                        + "\n".join(f"  {c}" for c in all_conflicts[:10])
                        + (f"\n  ... and {len(all_conflicts) - 10} more" if len(all_conflicts) > 10 else "")
                    )
                    return needs_confirm(_TOOL, "(batch transfer)", preview)

            results: List[Dict[str, Any]] = []
            all_warnings: List[str] = []
            for t in transfers:
                all_warnings.extend(t.get("glob_warnings", []))
                for p_str in t["paths"]:
                    results.append(self._manage_one(p_str, action, destination_dir=t["destination_dir"], permanent=permanent))

            return self._batch_result(action, results, all_warnings)

        # ── single destination ────────────────────────────────────────────────
        paths_raw: Optional[List[Any]] = args.get("paths")
        if not paths_raw or not isinstance(paths_raw, list):
            return err(_TOOL, msg=(
                f"manage {action} requires 'paths' — a non-empty list of absolute paths "
                "(even for a single item: [\"/path/to/file\"])."
            ))

        path_list = [resolve_path(str(r).strip()) for r in paths_raw if str(r).strip()]
        if not path_list:
            return err(_TOOL, msg="manage 'paths' resolved to an empty list — check the provided paths.")

        glob_warnings_single: List[str] = []
        if action != "mkdir":
            path_list = self._expand_globs(path_list, glob_warnings_single)
            if not path_list:
                msg = "manage: no files matched the given path(s)/pattern(s)"
                if glob_warnings_single:
                    msg += " — " + "; ".join(glob_warnings_single)
                return err(_TOOL, msg=msg)

        dest_raw = str(args.get("destination_dir") or "").strip()
        destination_dir = resolve_path(dest_raw) if dest_raw else None

        if action in ("copy", "move") and not destination_dir:
            return err(_TOOL, action=action, msg=(
                f"manage {action} requires 'destination_dir' — absolute path to the target directory."
            ))

        if not confirmed and action != "mkdir":
            if action == "delete":
                targets = [p for p in path_list if Path(p).exists()]
                if targets:
                    dest_word = "permanently delete" if permanent else "move to trash"
                    preview = (
                        f"manage delete: will {dest_word} {len(targets)} item(s):\n"
                        + "\n".join(f"  {p}" for p in targets[:10])
                        + (f"\n  ... and {len(targets) - 10} more" if len(targets) > 10 else "")
                    )
                    return needs_confirm(_TOOL, "(delete)", preview)

            elif action in ("copy", "move") and destination_dir:
                dest_p = Path(destination_dir)
                conflicts = (
                    [str(dest_p / Path(p).name) for p in path_list if (dest_p / Path(p).name).exists()]
                    if dest_p.is_dir()
                    else ([destination_dir] if dest_p.exists() else [])
                )
                if conflicts:
                    preview = (
                        f"manage {action}: will {action} {len(path_list)} item(s) → {destination_dir}\n"
                        f"{len(conflicts)} destination path(s) already exist and will be overwritten:\n"
                        + "\n".join(f"  {c}" for c in conflicts[:10])
                        + (f"\n  ... and {len(conflicts) - 10} more" if len(conflicts) > 10 else "")
                    )
                    return needs_confirm(_TOOL, "(overwrite)", preview)

        results_single = [
            self._manage_one(p_str, action, destination_dir=destination_dir, permanent=permanent)
            for p_str in path_list
        ]
        return self._batch_result(action, results_single, glob_warnings_single)

    def _batch_result(self, action: str, results: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
        succeeded = sum(1 for r in results if r.get("STATUS") == "success")
        out: Dict[str, Any] = {
            "STATUS": "success" if succeeded == len(results) else "failed",
            "TOOL": _TOOL,
            "ACTION": action,
            "TOTAL": len(results),
            "SUCCEEDED": succeeded,
            "FAILED": len(results) - succeeded,
            "RESULTS": results,
        }
        if warnings:
            out["GLOB_WARNINGS"] = warnings
        return out

    def _manage_one(
        self, path: str, action: str,
        destination_dir: Optional[str], permanent: bool = False,
    ) -> Dict[str, Any]:
        p = Path(path)
        try:
            if action == "mkdir":
                p.mkdir(parents=True, exist_ok=True)
                return ok(_TOOL, path=path, ACTION=action)

            if action == "delete":
                if not p.exists():
                    return ok(_TOOL, path=path, ACTION=action, NOTE="Already absent — nothing to delete.")
                if permanent:
                    shutil.rmtree(path) if p.is_dir() else p.unlink()
                else:
                    try:
                        import send2trash
                        send2trash.send2trash(path)
                    except ImportError:
                        shutil.rmtree(path) if p.is_dir() else p.unlink()
                return ok(_TOOL, path=path, ACTION=action, PERMANENT=permanent)

            if action in ("copy", "move"):
                if not destination_dir:
                    return err(_TOOL, path=path, action=action, msg=(
                        f"{action} failed on '{p.name}' — destination_dir is missing."
                    ))
                if not p.exists():
                    return err(_TOOL, path=path, action=action, msg=(
                        f"{action} failed — source '{path}' does not exist. "
                        "Verify the path from prior step output."
                    ))
                dest = Path(destination_dir)
                dest.mkdir(parents=True, exist_ok=True)
                actual_dest = dest / p.name
                if action == "copy":
                    shutil.copytree(path, str(actual_dest), dirs_exist_ok=True) if p.is_dir() else shutil.copy2(path, str(actual_dest))
                else:
                    shutil.move(path, str(actual_dest))
                return ok(_TOOL, path=path, ACTION=action, DESTINATION=str(actual_dest))

        except PermissionError as e:
            return err(_TOOL, path=path, action=action, msg=(
                f"{action} failed on '{Path(path).name}' — permission denied: {e}"
            ))
        except OSError as e:
            return err(_TOOL, path=path, action=action, msg=(
                f"{action} failed on '{Path(path).name}' — {type(e).__name__}: {e}"
            ))
        except Exception as e:
            return err(_TOOL, path=path, action=action, msg=(
                f"{action} failed on '{Path(path).name}' — {type(e).__name__}: {e}"
            ))

        return err(_TOOL, path=path, action=action, msg=f"unreachable state for action='{action}'")

    # ── rename ────────────────────────────────────────────────────────────────

    def _rename(self, args: Dict[str, Any]) -> Dict[str, Any]:
        renames_raw = args.get("renames")
        if not renames_raw or not isinstance(renames_raw, list):
            return err(_TOOL, action="rename", msg=(
                "rename requires 'renames' — a list of {path, new_name} objects "
                "(even for a single rename)."
            ))

        confirmed = bool(args.get("confirmed", False))

        items: List[Dict[str, Any]] = []
        for i, r in enumerate(renames_raw):
            path_raw = str(r.get("path") or "").strip()
            new_name = str(r.get("new_name") or "").strip()
            if not path_raw or not new_name:
                return err(_TOOL, action="rename", msg=(
                    f"renames[{i}] is missing 'path' or 'new_name' — both are required."
                ))
            if "/" in new_name or "\\" in new_name:
                return err(_TOOL, action="rename", msg=(
                    f"renames[{i}].new_name '{new_name}' contains a path separator. "
                    "new_name must be a filename only (no slashes). "
                    "To move AND rename, use manage action='move' instead."
                ))
            items.append({"path": resolve_path(path_raw), "new_name": new_name})

        if not confirmed:
            conflicts = [
                str(Path(item["path"]).parent / item["new_name"])
                for item in items
                if (Path(item["path"]).parent / item["new_name"]).exists()
            ]
            if conflicts:
                preview = (
                    f"rename: {len(items)} rename(s) requested. "
                    f"{len(conflicts)} target name(s) already exist and will be overwritten:\n"
                    + "\n".join(f"  {c}" for c in conflicts)
                )
                return needs_confirm(_TOOL, "(rename conflicts)", preview)

        results: List[Dict[str, Any]] = []
        for item in items:
            p = Path(item["path"])
            target = p.parent / item["new_name"]
            try:
                if not p.exists():
                    results.append(err(_TOOL, path=str(p), action="rename", msg=(
                        f"rename failed — '{item['path']}' does not exist."
                    )))
                    continue
                p.rename(target)
                results.append(ok(_TOOL, path=str(p), ACTION="rename", DESTINATION=str(target)))
            except PermissionError as e:
                results.append(err(_TOOL, path=str(p), action="rename", msg=(
                    f"rename failed on '{p.name}' → '{item['new_name']}' — permission denied: {e}"
                )))
            except OSError as e:
                results.append(err(_TOOL, path=str(p), action="rename", msg=(
                    f"rename failed on '{p.name}' → '{item['new_name']}' — {type(e).__name__}: {e}"
                )))

        succeeded = sum(1 for r in results if r.get("STATUS") == "success")
        return {
            "STATUS": "success" if succeeded == len(results) else "failed",
            "TOOL": _TOOL,
            "ACTION": "rename",
            "TOTAL": len(results),
            "SUCCEEDED": succeeded,
            "FAILED": len(results) - succeeded,
            "RESULTS": results,
        }


TOOL_CLASS = FsManage


def get_tool() -> FsManage:
    return FsManage()
