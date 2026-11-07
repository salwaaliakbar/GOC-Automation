"""GOC Register Data Collector.

Collects individual registrant details (name, town, date registered, practice
address) from the official General Optical Council register
(https://str.optical.org/). Every result is saved immediately in a SQLite
database (the source of truth); the Excel file is an export of it. Collections
can be paused, resumed and incrementally updated.
"""

__version__ = "1.0.0"

NOT_AVAILABLE = "Data not available"
# Written into the Excel sheet for numbers whose search failed (website error).
# Deliberately different from NOT_AVAILABLE so it is never mistaken for a
# confirmed "no record" when the workbook is imported again.
SEARCH_FAILED = "Search failed - retry required"
NOT_PROCESSED = "Not yet processed"
