"""
License Management Module for SmartLog TUI
Handles Lemon Squeezy API licensing, feature gating, and usage limits.
"""

from __future__ import annotations

import json
import os
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Lemon Squeezy Licensing API Endpoint
LEMON_SQUEEZY_ACTIVATE_URL = "https://api.lemonsqueezy.com/v1/licenses/activate"

# Default license file paths
DEFAULT_LICENSE_DIR = Path.home() / ".smartlog"
DEFAULT_LICENSE_FILE = DEFAULT_LICENSE_DIR / "license.json"
DEFAULT_SESSION_FILE = DEFAULT_LICENSE_DIR / "session.json"


class LicenseTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass
class LicenseConfig:
    """Configuration for license limits and features."""

    # Free tier limits
    free_ai_diagnoses_per_session: int = 3
    free_log_buffer_size: int = 100
    free_max_filters: int = 2

    # Feature flags
    features: dict[str, dict[str, bool]] = field(
        default_factory=lambda: {
            LicenseTier.FREE.value: {
                "ai_diagnostics": True,
                "export_html": False,
                "export_json": False,
                "custom_filters": False,
                "real_time_streaming": True,
                "error_detection": True,
            },
            LicenseTier.PRO.value: {
                "ai_diagnostics": True,
                "export_html": True,
                "export_json": True,
                "custom_filters": True,
                "real_time_streaming": True,
                "error_detection": True,
            },
            LicenseTier.ENTERPRISE.value: {
                "ai_diagnostics": True,
                "export_html": True,
                "export_json": True,
                "custom_filters": True,
                "real_time_streaming": True,
                "error_detection": True,
            },
        }
    )


# ==========================================
# Helpers & Security Functions
# ==========================================

def _secure_mkdir(path: Path) -> None:
    """Create directory with 0o700 permissions on POSIX systems."""
    path.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _is_date_expired(expires_at: Any) -> bool:
    """Safely check if an ISO 8601 string or numeric timestamp is in the past."""
    if not expires_at:
        return False

    current_ts = time.time()

    # If numeric timestamp
    if isinstance(expires_at, (int, float)):
        return current_ts > expires_at

    # If ISO 8601 string from Lemon Squeezy (e.g. "2025-12-31T23:59:59.000000Z")
    if isinstance(expires_at, str):
        try:
            clean_str = expires_at.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
            # Normalize to UTC timestamp
            return dt.timestamp() < current_ts
        except Exception:
            return False

    return False


def save_license(license_data: dict[str, Any], file_path: Path | None = None) -> bool:
    """Save validated license information to secure local storage."""
    target_path = file_path or DEFAULT_LICENSE_FILE
    try:
        _secure_mkdir(target_path.parent)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(license_data, f, indent=2)

        # Set file permissions to 0o600 on Linux/macOS
        if platform.system() != "Windows":
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
        return True
    except IOError as e:
        print(f"Warning: Could not save license file: {e}")
        return False


def load_license(file_path: Path | None = None) -> dict[str, Any] | None:
    """Load stored license information from local storage."""
    target_path = file_path or DEFAULT_LICENSE_FILE
    if not target_path.exists():
        return None
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, IOError):
        return None


def verify_license_key(
    license_key: str,
    instance_name: str | None = None,
    timeout_seconds: float = 6.0,
) -> dict[str, Any]:
    """Verify and activate a license key via Lemon Squeezy REST API."""
    cleaned_key = license_key.strip()
    if not cleaned_key:
        return {
            "valid": False,
            "message": "License key cannot be empty.",
            "data": {},
        }

    # Generate device instance name if not provided
    node_name = platform.node() or "Client"
    client_instance = instance_name or f"SmartLog_TUI_{node_name}"

    payload = {
        "license_key": cleaned_key,
        "instance_name": client_instance,
    }
    encoded_data = json.dumps(payload).encode("utf-8")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "SmartLog-TUI-LicenseManager/1.0",
    }

    req = urllib.request.Request(
        LEMON_SQUEEZY_ACTIVATE_URL,
        data=encoded_data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8")
            res_json = json.loads(raw_body)

            is_activated = res_json.get("activated", False)
            error_msg = res_json.get("error")

            if is_activated:
                license_info = res_json.get("license_key", {})
                meta = res_json.get("meta", {})
                product_name = meta.get("product_name", "SmartLog Pro")

                return {
                    "valid": True,
                    "message": f"License successfully activated for '{product_name}'!",
                    "data": {
                        "key": cleaned_key,
                        "status": license_info.get("status", "active"),
                        "instance_id": res_json.get("instance", {}).get("id"),
                        "product_name": product_name,
                        "customer_name": meta.get("customer_name") or meta.get("user_name"),
                        "customer_email": meta.get("customer_email") or meta.get("user_email"),
                        "expires_at": license_info.get("expires_at"),
                        "activated_at": time.time(),
                        "raw_response": res_json,
                    },
                }
            else:
                return {
                    "valid": False,
                    "message": error_msg or "License key is invalid or activation limit reached.",
                    "data": res_json,
                }

    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            msg = err_json.get("error") or err_json.get("message") or f"HTTP {e.code} Error"
        except Exception:
            msg = f"HTTP {e.code}: {e.reason}"
        return {
            "valid": False,
            "message": msg,
            "data": {},
        }
    except urllib.error.URLError as e:
        return {
            "valid": False,
            "message": f"Network connection failed: {e.reason}",
            "data": {},
        }
    except Exception as e:
        return {
            "valid": False,
            "message": f"An unexpected error occurred during verification: {str(e)}",
            "data": {},
        }


# ==========================================
# License Manager Class
# ==========================================

class LicenseManager:
    """Manages license verification, Lemon Squeezy activation, feature gating, and usage tracking."""

    DEMO_LICENSE_KEY = "SL-DEMO-2024-PRO-UNLIMITED"

    def __init__(
        self,
        config: LicenseConfig | None = None,
        license_file: Path | None = None,
        session_file: Path | None = None,
    ) -> None:
        self.config = config or LicenseConfig()
        self.license_file = license_file or DEFAULT_LICENSE_FILE
        self.session_file = session_file or DEFAULT_SESSION_FILE

        self._license: dict[str, Any] | None = None
        self._session_data: dict[str, Any] = {}
        self._is_initialized: bool = False

    def _ensure_directories(self) -> None:
        """Create necessary directories for storage."""
        _secure_mkdir(self.license_file.parent)

    def _generate_session_id(self) -> str:
        """Generate a secure unique session identifier."""
        return uuid.uuid4().hex[:16]

    def _ensure_initialized(self) -> None:
        """Ensure session and license are loaded consistently."""
        if not self._is_initialized:
            self._session_data = self._load_session()
            self.verify_license()
            self._is_initialized = True

    def _load_session(self) -> dict[str, Any]:
        """Load session data from file."""
        try:
            if self.session_file.exists():
                with open(self.session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Check if session has expired (1 hour TTL)
                if isinstance(data, dict) and data.get("created_at", 0) > time.time() - 3600:
                    return {
                        "session_id": data.get("session_id", self._generate_session_id()),
                        "created_at": data.get("created_at", time.time()),
                        "ai_diagnoses_count": data.get("ai_diagnoses_count", 0),
                        "tier": data.get("tier", LicenseTier.FREE.value),
                    }
        except (json.JSONDecodeError, IOError, TypeError):
            pass

        # Start new session
        return {
            "session_id": self._generate_session_id(),
            "created_at": time.time(),
            "ai_diagnoses_count": 0,
            "tier": LicenseTier.FREE.value,
        }

    def _save_session(self) -> None:
        """Save session data to file."""
        self._ensure_directories()
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(self._session_data, f, indent=2)
            if platform.system() != "Windows":
                try:
                    os.chmod(self.session_file, 0o600)
                except OSError:
                    pass
        except IOError as e:
            print(f"Warning: Could not save session data: {e}")

    def verify_license(self, license_key: str | None = None) -> dict[str, Any]:
        """Verify license status (offline cache or demo key)."""
        self._ensure_directories()

        # 1. Check for Demo Key
        if license_key == self.DEMO_LICENSE_KEY:
            self._license = {
                "valid": True,
                "tier": LicenseTier.PRO.value,
                "key": license_key,
                "expiry": None,
                "features": self.config.features[LicenseTier.PRO.value],
            }
            self._session_data["tier"] = LicenseTier.PRO.value
            return self._license

        # 2. Check for stored license on disk
        stored_license = load_license(self.license_file)
        if stored_license and not license_key:
            expires_at = stored_license.get("expires_at")
            is_expired = _is_date_expired(expires_at)

            if is_expired:
                self._license = {
                    "valid": False,
                    "tier": LicenseTier.FREE.value,
                    "error": "License expired",
                    "features": self.config.features[LicenseTier.FREE.value],
                }
                self._session_data["tier"] = LicenseTier.FREE.value
                self._save_session()
                return self._license

            tier_value = stored_license.get("tier", LicenseTier.PRO.value)
            self._license = {
                "valid": True,
                "tier": tier_value,
                "key": stored_license.get("key", ""),
                "customer": stored_license.get("customer_email") or stored_license.get("customer_name"),
                "features": self.config.features.get(
                    tier_value, self.config.features[LicenseTier.PRO.value]
                ),
            }
            self._session_data["tier"] = tier_value
            return self._license

        # 3. Default: Free tier mode
        self._license = {
            "valid": False,
            "tier": LicenseTier.FREE.value,
            "key": "",
            "features": self.config.features[LicenseTier.FREE.value],
        }
        self._session_data["tier"] = LicenseTier.FREE.value
        return self._license

    def activate_license(self, license_key: str) -> dict[str, Any]:
        """Activate a new license key via Lemon Squeezy API."""
        cleaned_key = license_key.strip()

        # Handle local Demo key
        if cleaned_key == self.DEMO_LICENSE_KEY:
            self._license = {
                "valid": True,
                "tier": LicenseTier.PRO.value,
                "key": cleaned_key,
                "expiry": None,
                "features": self.config.features[LicenseTier.PRO.value],
            }
            self._session_data["tier"] = LicenseTier.PRO.value
            self._save_session()
            save_license(
                {
                    "key": cleaned_key,
                    "tier": LicenseTier.PRO.value,
                    "product_name": "SmartLog Pro (Demo)",
                    "activated_at": time.time(),
                },
                self.license_file,
            )
            return {
                "success": True,
                "license": self._license,
                "message": "Demo Pro license activated successfully!",
            }

        # Verify online with Lemon Squeezy
        verification = verify_license_key(cleaned_key)

        if verification.get("valid", False):
            data = verification.get("data", {})
            product_name = str(data.get("product_name", "")).upper()

            # Determine Tier based on product name
            tier = LicenseTier.ENTERPRISE if "ENTERPRISE" in product_name else LicenseTier.PRO

            license_data = {
                "key": cleaned_key,
                "tier": tier.value,
                "product_name": data.get("product_name"),
                "customer_name": data.get("customer_name"),
                "customer_email": data.get("customer_email"),
                "instance_id": data.get("instance_id"),
                "activated_at": data.get("activated_at", time.time()),
                "expires_at": data.get("expires_at"),
            }

            save_license(license_data, self.license_file)

            self._license = {
                "valid": True,
                "tier": tier.value,
                "key": cleaned_key,
                "features": self.config.features[tier.value],
            }
            self._session_data["tier"] = tier.value
            self._save_session()

            return {
                "success": True,
                "license": self._license,
                "message": verification.get("message", "License activated successfully!"),
            }
        else:
            return {
                "success": False,
                "error": verification.get("message", "License verification failed."),
            }

    def get_tier(self) -> LicenseTier:
        """Get current license tier."""
        self._ensure_initialized()
        tier_value = self._session_data.get("tier", LicenseTier.FREE.value)
        try:
            return LicenseTier(tier_value)
        except ValueError:
            return LicenseTier.FREE

    def can_use_ai_diagnosis(self) -> bool:
        """Check if user can use AI diagnosis feature."""
        self._ensure_initialized()
        tier = self.get_tier()
        if tier == LicenseTier.FREE:
            count = self._session_data.get("ai_diagnoses_count", 0)
            return count < self.config.free_ai_diagnoses_per_session
        return True

    def use_ai_diagnosis(self) -> dict[str, Any]:
        """Consume one AI diagnosis usage."""
        self._ensure_initialized()
        tier = self.get_tier()

        if tier == LicenseTier.FREE:
            current_count = self._session_data.get("ai_diagnoses_count", 0)
            if current_count >= self.config.free_ai_diagnoses_per_session:
                return {
                    "allowed": False,
                    "remaining": 0,
                    "message": "Free tier limit reached. Upgrade to Pro for unlimited AI diagnoses.",
                }

            self._session_data["ai_diagnoses_count"] = current_count + 1
            self._save_session()
            remaining = self.config.free_ai_diagnoses_per_session - current_count - 1

            return {
                "allowed": True,
                "remaining": remaining,
                "message": f"AI diagnosis used. {remaining} remaining this session.",
            }
        else:
            return {
                "allowed": True,
                "remaining": float("inf"),
                "message": f"{tier.value.capitalize()} tier - unlimited AI diagnoses.",
            }

    def get_feature_enabled(self, feature: str) -> bool:
        """Check if a specific feature is enabled for current tier."""
        tier = self.get_tier()
        return self.config.features.get(tier.value, {}).get(feature, False)

    def get_log_buffer_limit(self) -> int:
        """Get maximum log buffer size for current tier."""
        tier = self.get_tier()
        if tier == LicenseTier.FREE:
            return self.config.free_log_buffer_size
        return 10000  # Pro/Enterprise have larger buffers

    def reset_session(self) -> None:
        """Reset current session."""
        self._ensure_initialized()
        self._session_data["session_id"] = self._generate_session_id()
        self._session_data["created_at"] = time.time()
        self._session_data["ai_diagnoses_count"] = 0
        self._save_session()

    def _get_ai_remaining(self) -> int | float:
        """Get remaining AI diagnoses for current session."""
        tier = self.get_tier()
        if tier == LicenseTier.FREE:
            count = self._session_data.get("ai_diagnoses_count", 0)
            return max(0, self.config.free_ai_diagnoses_per_session - count)
        return float("inf")

    def get_license_info(self) -> dict[str, Any]:
        """Get comprehensive license information."""
        tier = self.get_tier()
        remaining = self._get_ai_remaining()

        return {
            "tier": tier.value,
            "is_pro": tier != LicenseTier.FREE,
            "ai_diagnoses_remaining": "unlimited" if remaining == float("inf") else remaining,
            "log_buffer_limit": self.get_log_buffer_limit(),
            "features": self.config.features[tier.value],
        }


# Singleton instance
_license_manager: LicenseManager | None = None


def get_license_manager() -> LicenseManager:
    """Get or create the singleton LicenseManager instance."""
    global _license_manager
    if _license_manager is None:
        _license_manager = LicenseManager()
        _license_manager._ensure_initialized()
    return _license_manager
