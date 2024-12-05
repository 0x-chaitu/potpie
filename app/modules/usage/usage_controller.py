from datetime import date
from app.modules.usage.usage_service import UsageService
from app.modules.usage.usage_schema import UsageResponse

class UsageController:
    @staticmethod
    async def get_user_usage(start_date: date, end_date: date, user_id: str) -> UsageResponse:        
        usage_data = await UsageService.get_usage_data(start_date, end_date, user_id)
        return usage_data 