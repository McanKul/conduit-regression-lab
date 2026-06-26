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
2. **Spec şemaları `$ref` ile components'te.** Bir response DTO'suna alan eklemek `paths`'i değil `components.schemas`'ı değiştirir; bu yüzden `spec_diff` **şema-farkında**: her path'in transitively kullandığı şemaların closure'ını karşılaştırır. Kaynak etkisi ise regex ile değil, MSBuild'in derlediği solution üzerinde Roslyn sembolleriyle bulunur.

## Klasör (fork içinde)
```
.github/workflows/api-regression.yml          # pipeline
tools/api-regression/
  impact_resolver.py  impact.config.json        # hangi endpoint test edilecek
  golden_runner.py    corpus.realworld.json  normalize.config.json
tools/DotnetApiRegression.Analyzer/              # Roslyn/MSBuild dotnet tool
goldens/                                       # COMMIT'LENİR (capture üretir)
GOLDEN.md  KULLANIM.md
.gitignore  ->  specs/  schemathesis-report/  golden-report.json
```

## Akış (her PR)
```
compose up Postgres (ephemeral)
  → base app + PR app çalışır, OpenAPI runtime/static/build yoluyla alınır
  → base + PR için Roslyn semantic index üretilir
  → impact_resolver  (spec diff + sembol grafiği → impacted-paths.txt)
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
`CommandArticles.cs` imzayı değiştirmeden değişti; spec aynı ama Roslyn'in
implementation/interface ilişkisi ve ters sembol grafiğiyle
`ArticlesEndpoints.cs`'e ulaşılır:
```json
{
  "global": false,
  "rebaseline_entities": [],
  "endpoints": [
    {"path": "/articles", "reasons": ["semantic dependency on src/Conduit.Application/Features/Articles/Commands/CommandArticles.cs via src/Conduit.Presentation/EndPoints/ArticlesEndpoints.cs"]},
    {"path": "/articles/{slug}", "reasons": ["semantic dependency on src/Conduit.Application/Features/Articles/Commands/CommandArticles.cs via src/Conduit.Presentation/EndPoints/ArticlesEndpoints.cs"]}
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

CLI doğrudan çalıştırıldığında `verify`, impacted endpoint'te fark varsa non-zero
exit code döndürür; gerisi uyarıdır. Bu repodaki GitHub workflow'u golden
sonucunu PR yorumu olarak göstermek için bilerek `|| true` ile advisory bırakır.

## Paketleme

Python orkestratörü ve Roslyn analizörü ayrı paketlenir:

```sh
pipx install ./tools/api-regression

dotnet pack tools/DotnetApiRegression.Analyzer \
  -c Release -o tools/api-regression/dist
dotnet tool install --global DotnetApiRegression.Analyzer \
  --add-source tools/api-regression/dist
```

Repo içinde geliştirme yaparken `analyze`, yerel analyzer projesini otomatik
bulur; global tool kurmak gerekmez. Tek komut yüzeyi:

```sh
dotnet-api-regression analyze \
  --root . --project src/Conduit.WebUI/Conduit.WebUI.csproj \
  --output specs/revision-semantic-index.json

dotnet-api-regression impact \
  --base origin/main --head HEAD \
  --spec specs/revision.json --base-spec specs/base.json \
  --semantic-index specs/revision-semantic-index.json \
  --base-semantic-index specs/base-semantic-index.json \
  --config tools/api-regression/impact.config.json \
  --out-dir specs

dotnet-api-regression golden verify \
  --corpus tools/api-regression/corpus.realworld.json \
  --base-url http://localhost:5000/api \
  --normalize tools/api-regression/normalize.config.json \
  --goldens goldens \
  --gate-paths specs/impacted-paths.txt
```

## Büyük ASP.NET Core projeleri

`DotnetApiRegression.Analyzer`, solution'ı `MSBuildWorkspace` ile açar ve
compiler'ın çözdüğü sembollerden deterministik bir manifest üretir:

- `MapGet/Post/Put/Delete/Patch/Methods` çağrıları Roslyn method symbol'üyle
  doğrulanır; aynı isimli kullanıcı metotları endpoint sayılmaz.
- Route sabitleri compiler constant evaluation ile çözülür. İç içe
  `MapGroup` değişkenleri takip edilir.
- Controller route'ları derlenmiş attribute verisinden okunur.
- Interface/implementation, partial type ve dosyalar arası method/type
  referanslarından ters bağımlılık grafiği çıkarılır.
- Endpoint route'u dinamikse `operationId`/`WithName` eşleşmesi denenir.
- Base ve revision manifestleri birlikte okunur; silinen veya taşınan kaynak
  dosyaları kaybolmaz.

Grafik bir kez, lineer kaynak taramasıyla kurulur. Impact aşaması yalnız değişen
dosyalardan ters BFS yapar; endpoint sayısına göre bütün solution'ı tekrar
aramaz. `impact-report.json` içinde kullanılan motor `source_analysis: "roslyn"`
olarak ve analyzer istatistikleri `stats.semantic` altında görünür.
CI'da web entry project'ini `--project` ile vermek, onun project-reference
zincirini yüklerken alakasız test/tool projelerini dışarıda bırakır. Birden fazla
bağımsız API host'u varsa her host için manifest üret; `--semantic-index` ve
`--base-semantic-index` seçenekleri tekrar edilebilir.

Güvenlik/hız dengesi `impact.config.json` ile seçilir:

```json
{
  "source_analysis_mode": "semantic",
  "semantic_max_hops": 32,
  "semantic_unresolved_strategy": "all",
  "global_impact_strategy": "all",
  "fallback_all_when_unresolved": true
}
```

- `source_analysis_mode: "semantic"`: Roslyn manifesti olmadan job hata verir.
- `semantic_unresolved_strategy: "all"`: dinamik/reflection tabanlı bir endpoint
  route'u çözülemezse bütün endpoint'leri güvenli tarafta impacted yapar.
- `global_impact_strategy: "all"`: global trigger varsa bütün endpoint'ler test edilir.
- `semantic_max_hops`: katmanlı mimaride ters sembol grafiğinin üst sınırıdır.
- `fallback_all_when_unresolved: true`: değişen uygulama kaynağı hiçbir
  endpoint'e bağlanamazsa sessizce test atlamak yerine tam suite'e düşer.

## Swagger yoksa OpenAPI

Pipeline Swagger UI'a bağlı değildir. `openapi capture` sırasıyla çalışan
uygulamanın URL'sini, repodaki OpenAPI JSON'u ve ASP.NET build-time generator'ı
dener:

```sh
dotnet-api-regression openapi capture \
  --output specs/revision.json \
  --url http://localhost:5000/api/v1/swagger.json \
  --wait-seconds 180 \
  --search-root . \
  --project src/Conduit.WebUI/Conduit.WebUI.csproj
```

Build fallback için web projesinde `Microsoft.Extensions.ApiDescription.Server`
paketi bulunmalı ve uygulama `AddOpenApi` ile doküman kaydetmelidir. Bu repoda
paket `PrivateAssets=all` olarak eklendi; normal build'de üretim kapalıdır,
capture komutu gerektiğinde `OpenApiGenerateDocuments=true` ile açar. Yalnız
hazır bir OpenAPI dosyası olan projede `--file path/to/openapi.json --no-build`
yeterlidir.

## Başka bir .NET projesine taşıma
Python ve dotnet tool paketlerini kur, workflow'da base/revision semantic
manifestlerini üret ve `impact.config.json` dosyasını tune et. `corpus.*.json`
projeye özeldir. JWT-Bearer kullanan API'lerde corpus `auth_scheme`'i `Bearer`
yap. Spec `/api` gibi bir server prefix taşımıyorsa `--base-url`'den prefix'i
kaldır.
