"""
SNLA Session State Manager

Maintains in-memory state for multi-turn conversation sessions.
MVP phase: No persistence to disk. All state is lost on app restart.
"""

from dataclasses import dataclass, field


@dataclass
class SessionState:
    """
    Tracks all state for a single analysis session.

    MVP phase: In-memory only, no database persistence.
    Designed for Streamlit's session_state pattern.
    """

    # ===== Dataset State =====
    dataset_meta: dict = field(default_factory=dict)
    # Populated after file upload: {"filename": str, "format": "sav"|"csv", "row_count": int, "column_count": int, "file_path": str}

    variables: list[dict] = field(default_factory=list)
    # [{"name": str, "type": str, "label": str, "value_labels": dict|None, "desensitized": bool|None, "original_name": str|None}]

    # ===== Variable Name Mapping (Privacy) =====
    var_name_map: dict[str, str] = field(default_factory=dict)
    # Desensitized name → Original name (e.g., {"var_01": "患者姓名"})

    reverse_var_name_map: dict[str, str] = field(default_factory=dict)
    # Original name → Desensitized name (e.g., {"患者姓名": "var_01"})
    # Used only when sending to cloud LLM

    @property
    def has_data(self) -> bool:
        """Whether a dataset has been loaded."""
        return bool(self.variables)

    # ===== Conversation State =====
    history: list[dict] = field(default_factory=list)
    # Each entry: {"role": "user"|"assistant", "content": str, "timestamp": str, "analysis": dict|None}

    last_analysis: dict | None = None
    # Context from the most recent analysis for follow-up detection
    # {"method": str, "grouping_var": str|None, "test_var": str|None, "analysis_type": str}

    # ===== Active Operations =====
    active_syntax: str | None = None
    # Currently active SPSS syntax (for user review before execution)

    active_process: "Popen | None" = None  # type: ignore
    # Handle to the running SPSS subprocess (for cancellation)

    cancellation_token: bool = False
    # Set to True when user requests cancellation. Checked by executor in poll loop.

    # ===== Temporary File Tracking =====
    temp_files: list[str] = field(default_factory=list)
    # Paths to temporary data copies (cleaned up on session end)

    # ===== UI State =====
    current_stage: str = "UPLOADING"
    # One of: UPLOADING, READY, THINKING, EXECUTING, DONE, ERROR

    error_message: str | None = None
    # Current error message if stage is ERROR

    # ===== Methods =====

    def add_message(self, role: str, content: str, analysis: dict | None = None):
        """Add a message to conversation history."""
        from datetime import datetime

        self.history.append(
            {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "analysis": analysis,
            }
        )

    def set_last_analysis(
        self,
        method: str,
        grouping_var: str | None = None,
        test_var: str | None = None,
        analysis_type: str = "",
    ):
        """Record context from the most recent analysis for follow-up detection."""
        self.last_analysis = {
            "method": method,
            "grouping_var": grouping_var,
            "test_var": test_var,
            "analysis_type": analysis_type,
        }

    def get_variable_names(self) -> list[str]:
        """Get just the list of current variable names (for validator input)."""
        return [v["name"] for v in self.variables]

    def get_variable(self, name: str) -> dict | None:
        """Find a variable by name."""
        for v in self.variables:
            if v["name"] == name:
                return v
        return None

    def cancel(self):
        """Request cancellation of the current operation."""
        self.cancellation_token = True

    def reset_cancellation(self):
        """Reset the cancellation token for a new operation."""
        self.cancellation_token = False

    def map_to_cloud(self, variables: list[dict] | None = None) -> list[dict]:
        """
        Map variable names from original to desensitized for cloud LLM request.

        Only variable names that have been desensitized (have original_name) are mapped.

        Args:
            variables: Variable list to map. Uses self.variables if None.

        Returns:
            Variable list with names mapped to cloud-safe versions
        """
        vars_to_map = variables if variables is not None else self.variables
        mapped = []
        for v in vars_to_map:
            new_v = dict(v)
            # Use desensitized name if this variable was renamed
            if v.get("desensitized") and v.get("original_name"):
                new_v["name"] = v["name"]  # Already desensitized name
                new_v["_original_name"] = v["original_name"]
            mapped.append(new_v)
        return mapped

    def map_to_local(self, syntax: str) -> str:
        """
        Map desensitized variable names in LLM-generated syntax back to original names.

        Scans the syntax string and replaces var_01, var_02, etc. with their original names
        so SPSS execution uses the correct variable names.

        Args:
            syntax: SPSS syntax string potentially containing desensitized names

        Returns:
            Syntax string with original variable names restored
        """
        result = syntax
        # Sort by key length descending to avoid partial replacements (var_1 vs var_10)
        for cloud_name in sorted(self.var_name_map.keys(), key=len, reverse=True):
            original = self.var_name_map[cloud_name]
            result = result.replace(cloud_name, original)
        return result

    def register_temp_file(self, file_path: str):
        """Register a temporary file for cleanup on session end."""
        if file_path not in self.temp_files:
            self.temp_files.append(file_path)

    def cleanup(self):
        """Clean up all temporary files."""
        import os

        for f in self.temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self.temp_files.clear()

    def reset(self):
        """Reset session state for a new dataset."""
        self.cleanup()
        self.dataset_meta = {}
        self.variables = []
        self.var_name_map = {}
        self.reverse_var_name_map = {}
        self.history = []
        self.last_analysis = None
        self.active_syntax = None
        self.active_process = None
        self.cancellation_token = False
        self.current_stage = "UPLOADING"
        self.error_message = None
