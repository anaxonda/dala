from abc import ABC, abstractmethod
from typing import Optional
from . .models import BookData, ConversionContext, Source

class BaseDriver(ABC):
    @abstractmethod
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        pass
