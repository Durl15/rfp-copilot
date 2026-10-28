Day 6b: remove real-world names from demo data.
1. In app/seed.py, tests and README, replace every real agency or place-based client in demo data with clearly invented names ending in (fictional): Pinewood County Facilities, Lakeview County Public Works, Maple Ridge Central School District. Replace named references with role-only text, for example Reference: Facilities Director (fictional). Never use real person names in demo data.
2. Demo solicitation text and title must name only invented agencies. Keep the place of performance as Syracuse, NY.
3. Search the whole repo for Onondaga, Madison County, Syracuse City School and any real agency name; remove or replace each one. Real names are allowed only in the README sentence that says what is fictional.
4. Update tests that assert on the old names. All green.
Commit: Day 6b: fictional demo entities only. Do not push.
