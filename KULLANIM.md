# Kullanım — Impact-temelli API Regresyon Pipeline'ı

## Ne yapıyor
Her PR'da **değişen endpoint'leri bulup sadece onları** test eder. İki katman:
- **Schemathesis (fuzz):** çökme (5xx), şema ihlali, contract kırılması.
- **Golden:** happy-path cevabını kilitler, **sessiz davranış değişimini** (aynı status, farklı gövde) yakalar.

Önceden yazılmış test/assertion gerekmez — uygulamanın davranışı tek doğru kaynak.

## Neden hızlı (incremental)
- **İlk iterasyon:** corpus bir kez `capture` edilir → `goldens/` baseline'ı oluşur, "mutlak doğru" kabul edilir.
- **Sonraki her PR:** resolver `impact = spec-diff ∪ source-impact ∪ migration-impact` kümesini çıkarır; iki katman da **yalnız bu kümeye** koşar. 400 endpoint olsa bile tam tarama yok.

## Auth akışları + mock data
Corpus kendi (mock) datasını üretir ve auth akışını test eder: `register` → token al → `login` → korumalı endpoint'ler (`auth: true`) + token'sız çağrıda **401** (yetki zorlaması). Seed'deki rastgele veriye bağlı değildir, bu yüzden tekrarlanabilir.

## Bu repoya özgü 2 önemli gerçek
1. **`/api` öneki base URL'de.** Endpoint'ler `MapGroup("/api")` altında ama OpenAPI dokümanı server-relative (`servers:[{url:"/api"}]`, path'ler `/api`'siz). Bu yüzden corpus path'leri spec-relative (`/articles`), `/api` öneki `--base-url`'de (`http://localhost:5000/api`). Tek string her yerde: corpus = spec = impacted = schemathesis include.
2. **Spec şemaları `$ref` ile components'te.** Bir response DTO'suna alan eklemek `paths`'i değil `components.schemas`'ı değiştirir; bu yüzden `spec_diff` **şema-farkında**: her path'in transitively kullandığı şemaların closure'ını karşılaştırır. Ayrıca temiz mimaride endpoint'ler **interface** enjekte ettiği için (`ICommandArticles`), `source_impact` impl→interface köprüsü kurar (`implements_regex`).

## Klasör (fork içinde)
```
.github/workflows/api-regression.yml          # pipeline
tools/api-regression/
  impact_resolver.py  impact.config.json        # hangi endpoint test edilecek
  golden_runner.py    corpus.realworld.json  normalize.config.json
goldens/                                       # COMMIT'LENİR (capture üretir)
GOLDEN.md  KULLANIM.md
.gitignore  ->  specs/  schemathesis-report/  golden-report.json
```

## Akış (her PR)
```
compose up Postgres (ephemeral)
  → base app + PR app çalışır, /api/v1/swagger.json spec'leri çekilir
  → impact_resolver  (impacted-paths.txt)
  → oasdiff breaking gate (fail-on: ERR)
  → throwaway kullanıcı + token (Authorization: Token <jwt>)
  → Schemathesis  (yalnız impacted)
  → golden verify  (yalnız impacted)
compose down -v   (ephemeral DB)
```

---

## Üç sinyal — örnek çıktılar

### 1) Bir response DTO'suna alan eklendi → spec-diff
`ArticleDto`'ya alan eklemek `components.schemas.ArticleDto`'yu değiştirir; şema-farkında diff bunu `ArticleDto` kullanan tüm path'lere atfeder:
```
N endpoint(s) impacted | global=false | rebaseline entities=[]
  /articles            <-  spec: definition or schema changed
  /articles/{slug}     <-  spec: definition or schema changed
```

### 2) Bir handler'ın iç mantığı değişti (spec aynı) → source-impact
`CommandArticles.cs` imzayı değiştirmeden değişti; spec aynı ama impl→`ICommandArticles`→`ArticlesEndpoints.cs` köprüsüyle yakalanır:
```json
{
  "global": false,
  "rebaseline_entities": [],
  "endpoints": [
    {"path": "/articles", "reasons": ["depends on ICommandArticles (via src/Conduit.Application/Features/Articles/Commands/CommandArticles.cs)"]},
    {"path": "/articles/{slug}", "reasons": ["depends on ICommandArticles (via src/Conduit.Application/Features/Articles/Commands/CommandArticles.cs)"]}
  ],
  "changed_files": ["src/Conduit.Application/Features/Articles/Commands/CommandArticles.cs"]
}
```

### 3) Bir EF migration eklendi → migration-impact + rebaseline_entities
```
... | global=false | rebaseline entities=['Article']
```
`rebaseline_entities` dolu → o entity'lere dokunan endpoint'lerin golden'ları kasıtlı değişimde re-bless edilmeli.

### Kilitlenmiş golden (örnek `goldens/get-article.json`)
Oynak alanlar maskeli, anlamlı alanlar sabit:
```json
{
  "id": "get-article",
  "method": "GET",
  "path": "/articles/{slug}",
  "status": 200,
  "body": {
    "article": {
      "author": {"bio": null, "following": false, "image": null, "username": "goldenuser"},
      "title": "Golden Title",
      "description": "Golden description",
      "body": "Golden body",
      "tagList": ["golden"],
      "favorited": false,
      "favoritesCount": 0,
      "slug": "<masked>",
      "createdAt": "<masked>",
      "updatedAt": "<masked>"
    }
  }
}
```

### Davranış sessizce değişti → `golden-report.json`
Status aynı (200) ama gövde değişmiş; endpoint impacted olduğu için **fatal**:
```json
{
  "diffs": [
    {"id": "get-article", "path": "/articles/{slug}", "impacted": true,
     "status_changed": false, "expected_status": 200, "actual_status": 200, "body_changed": true}
  ],
  "fatal": [
    {"id": "get-article", "path": "/articles/{slug}", "impacted": true, "body_changed": true}
  ]
}
```

---

## Komutlar
Baseline alma ve re-bless → **GOLDEN.md** (Docker ile, lokal Python gerekmez).

PR'da `verify` otomatik koşar; impacted endpoint'te fark varsa **fail**, gerisi **uyarı**.

## Başka bir .NET projesine taşıma
`impact_resolver.py`, `golden_runner.py`, `normalize.config.json` generic. `impact.config.json` tune edilir, `corpus.*.json` projeye özeldir, workflow adapte edilir. JWT-Bearer kullanan API'lerde corpus `auth_scheme`'i `Bearer` yap (veya `--auth-scheme Bearer`). Spec `/api` gibi bir server prefix taşımıyorsa `--base-url`'den prefix'i kaldır.
