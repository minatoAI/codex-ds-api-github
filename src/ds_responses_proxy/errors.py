from fastapi import HTTPException


def error_body(message: str, error_type: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": error_type}}


def bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=error_body(message)["error"])

