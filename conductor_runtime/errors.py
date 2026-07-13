class ConductorError(Exception):
    """Base error for runtime failures."""


class ValidationError(ConductorError):
    """Raised when a workflow is malformed."""


class PolicyError(ConductorError):
    """Raised when a step violates the active runtime policy."""


class StepExecutionError(ConductorError):
    """Raised when an executable step fails."""


class ModelPlannerError(StepExecutionError):
    """Base class for model-workflow planner failures."""


class ModelPlannerLaunchError(ModelPlannerError):
    """Raised when the planner process cannot be launched."""


class ModelPlannerTimeoutError(ModelPlannerError):
    """Raised when the planner exceeds its process timeout."""


class ModelPlannerProviderError(ModelPlannerError):
    """Raised when the planner provider exits unsuccessfully."""


class ModelPlannerOutputLimitError(ModelPlannerError):
    """Raised when planner process output exceeds its local limit."""


class ModelPlannerTelemetryError(ModelPlannerError, ValidationError):
    """Raised when planner provider telemetry is malformed."""


class ModelPlannerSessionError(ModelPlannerError):
    """Raised when planner session identity cannot be proven."""


class ModelPlannerOutputError(ModelPlannerError, ValidationError):
    """Raised when the planner does not produce a valid workflow object."""
