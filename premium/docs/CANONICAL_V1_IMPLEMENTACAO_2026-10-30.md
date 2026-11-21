# Leitura geral e apresentação premium: implementação inicial

## Complementação conservadora de evidências — 21/11/2026

Antes da validação, `app/entity_evidence.py` pode acrescentar uma referência literal
para nomes de entidades públicas (`property`, `location`, `provider`). As evidências
anteriores precisam ser válidas e a localidade já precisa estar comprovada. A entidade
deve pertencer a um único serviço válido, sem outro nome concorrente no mesmo papel.
As páginas candidatas devem estar associadas exclusivamente a esse serviço por
evidências válidas ou por códigos já conferidos. A referência adicional vem de um
nome isolado na fonte ou de outra entidade comprovada com o mesmo nome no serviço;
blocos citados como condições são excluídos. Menções soltas em cláusulas ou páginas
de outro serviço não são suficientes.

Nenhum nome, localidade, valor, passageiro ou vínculo de código é alterado. A validação
original continua sendo executada e mantém os bloqueios. Cada complemento fica em
`data.evidence_repairs`, com entidade, origem, bloco acrescentado e versão da política.
O snapshot pago e a extração original permanecem imutáveis. Reconstruções HTML com
`retry-cache` podem reaplicar a política ao snapshot verificado, sem chamar o provedor;
cache ausente ou adulterado continua causando falha, sem geração paga automática.

Os testes sintéticos cobrem ambiguidade, citações inválidas, pessoas, condições,
localidade não comprovada, páginas compartilhadas, preservação dos dados, idempotência
e uma reconstrução HTTP/worker sem chave de IA. Evidências de clientes e instruções
de hospedagem ficam somente fora do repositório.

Data: 30/10/2026. Pipeline: `canonical_v1`, habilitado explicitamente para novos
uploads em modo HTML. O padrão de configuração continua `legacy`; os documentos
anteriores conservam a versão gravada. O `.env` privado não foi alterado.

## O que foi implementado

- Extração física genérica, sem caminhos condicionados a Universal, Comandatuba,
  Campos do Jordão, nome do arquivo ou catálogo de fornecedores. A versão anterior
  é mantida para seus registros e caches.
- Qualidade da camada de texto, texto bruto/coordenadas e OCR espacial para páginas
  escaneadas/mistas. Preservação do texto nativo útil e acréscimo de regiões OCR.
  Página vazia isolada é inventariada; um documento totalmente vazio é recusado.
- Uma leitura estruturada separa entidades, serviços, fatos, condições e associações
  de códigos. O esquema não pede páginas, títulos promocionais, HTML ou CSS à IA.
- Evidências conferidas na fonte, com limites de palavras/números, sinais financeiros,
  zeros iniciais e relações entre IDs. Trecho recortado de `100` não comprova `10`.
- Verificação do titular/número/serviço do ingresso independente do título estético.
  Múltiplas pessoas na mesma página exigem vínculo local inequívoco; ambiguidade
  mantém o rascunho e bloqueia sua liberação.
- Código com indício visual de leitura incompleta recebe pendência. Pixels/payloads
  continuam extraídos e verificados pelo worker; a IA não gera os códigos.
- Cards a partir dos fatos, incluindo voo estruturado quando há campos suficientes,
  contatos identificados e trechos residuais preservados literalmente. Rótulo livre
  sem apoio na fonte não acrescenta uma vantagem/condição à apresentação.
- Agrupamento de cards por espaço e paginação medida no navegador, com continuação
  de seções e sem reduzir indefinidamente a fonte. Códigos independentes não viram
  um único código/card. A versão antiga de layout mantém sua regra anterior.
- Cores de inclusão/atenção conservadoras, slogan, DifBot, paleta, texto nativo e
  cards sem sombras. Trechos técnicos não são adicionados ao texto do cliente.
- Parser de fotos tolerante a `@type` em string/lista/objeto/null; falhas opcionais
  de busca/seleção registram causa sanitizada e usam identidade institucional.
- Entidade pública de lugar e sua localidade são separadas do nome do produto e
  dos passageiros. Busca continua opcional; nenhuma foto Google foi habilitada.
- Checkpoint imutável de leitura, estágio por documento e diagnóstico com módulo,
  linha e função, sem texto da exceção, locais, segredos ou URL de acesso.
- Correção por campo com evidência, revisão, ETag e auditoria, reutilizando o
  checkpoint e sem outra chamada paga. Novo processamento deliberado pode solicitar
  uma leitura nova; correções de apresentação e continuação usam cache.
- Entrada `legacy_pdf_url` por organização para o destino do botão final, com
  verificação de HTTPS/origem exata e inclusão na idempotência. Não há fetch/proxy
  desse endereço no renderizador. Links externos não recebem validade artificial.

API original, dados/configuração Bubble e serviços de produção permanecem intactos.
Os endpoints premium existentes continuam em `/v2`; os campos novos são aditivos.
O esquema usa o JSON já existente no banco, sem nova coluna/tabela nesta versão.

## Configuração para novos uploads

API e worker precisam usar HTML e a mesma área privada de dados/banco premium.
Usar um ambiente de homologação separado; não executar esta configuração no serviço
da API original. Exemplo de variáveis do processo, sem sobrescrever `.env`:

```powershell
$env:PDF_RENDER_MODE='html_template'
$env:PDF_PIPELINE_VERSION='canonical_v1'
$env:PDF_MAX_OUTPUT_PAGES='100'
$env:PDF_MEDIA_MODE='brand'
$env:PDF_AI_DOCUMENT_BUDGET_USD='0.10'
$env:PDF_AI_DAILY_BUDGET_USD='0.10'
```

Os US$ 0,10 são uma reserva estimada para uma leitura no piloto, não uma tarifa
nem limite da fatura do provedor. Definir limites diferentes deliberadamente para
um lote. O restante do ambiente privado continua necessário: autenticação, chave
de IA, banco, armazenamento, navegador e origem pública própria.

O modo de marca evita busca paga de foto e geração paga de imagem. Para a descoberta
opcional, usar `PDF_MEDIA_MODE=search` com as políticas/orçamentos já documentados.
Não copiar a credencial do provedor para o Bubble/navegador ou para logs.

### Botão do PDF legado

Configurar no novo ambiente a lista exata de hosts por organização, por exemplo:

```dotenv
PDF_LEGACY_PDF_HOSTS={"organizacao-exemplo":["files.example.com"]}
PDF_REQUIRE_LEGACY_PDF_URL=true
```

O exemplo não é o domínio real do cliente. O tenant vem da autenticação. O upload
recebe o PDF fonte em `input_pdf` e a URL padrão em `legacy_pdf_url`. Origem permitida
não comprova vínculo com a reserva: o consumidor Bubble deve garantir que ambos
pertencem ao mesmo voucher, usando também sua referência externa.

Sem esse perfil obrigatório, o piloto local mantém o acesso restrito à fonte para
o botão. Link legado e link interno têm políticas distintas. A disponibilidade e
retenção do arquivo legado continuam responsabilidades do fluxo que o hospeda.

## Correção de fatos e reconstrução

O JSON privado de `/review` inclui `data.understanding`, evidências, checkpoint e
`checks.canonical`. `processing_stage` no status informa o estágio, sem expor dados
dos passageiros. A decisão final ainda exige revisão, hash, `If-Match` e ausência
de pendências bloqueantes.

Novo endpoint autenticado de revisor:

```http
PATCH /v2/documents/{id}/facts
If-Match: <ETag atual>
```

```json
{
  "reason": "Conferência do campo na fonte",
  "changes": [{
    "fact_id": "identificador-do-campo",
    "raw_value": "valor literal da fonte",
    "evidence": [{"block_id": "p1-b2", "quote": "trecho literal da fonte"}]
  }]
}
```

Pode corrigir também `service_id`/`entity_id` do fato quando houver relação válida.
O endpoint recusa valor/evidência incompatível e cria nova revisão auditada. A
resposta bruta paga e o documento fonte são imutáveis. Snapshot ausente/adulterado
gera pendência e não dispara leitura nova automaticamente.

`/continue`, `/template` e `/retry-cache` mantêm o contrato existente. Para a nova
versão, a leitura vem do checkpoint conferido; mudar CSS não exige outra leitura.

## Verificação executada

- Suíte completa do candidato: **212 aprovados, 1 ignorado**, PostgreSQL real sem
  configuração de banco de teste. Um aviso de depreciação Starlette/httpx.
- Casos novos cobrem serviço sem fornecedor cadastrado, locação com menções de
  hotelaria, fatos sem evidência, benefício inventado em rótulo, nomes/códigos
  cruzados, sinais financeiros, comparação de números, OCR misto, JSON-LD variável,
  cache adulterado, origem de URL e continuação de texto sem corte.
- API/worker via HTTP em processos separados, usando **provedor local simulado**,
  fonte sintética de locação e zero requisições ao provedor real. PDF preservou
  bytes/SHA da resposta, o tipo `car_rental`, condições gerais e revisão obrigatória.
  O provedor simulado é desligado ao terminar e nunca integra a configuração real.
- Candidato sintético em uma página conferido com Poppler: sem corte/sobreposição.
  Essa evidência prova composição/contrato; não mede interpretação da IA em produção.

Relatórios privados: `var/test-results/canonical-implementation-complete-20261030.xml`
e `var/canonical-implementation-20261030/http-release-candidate/report.json`.

### Piloto real controlado

Após a bateria sintética foi enviado por HTTP um bilhete aéreo de uma página do
acervo autorizado, que o fluxo anterior classificava como desconhecido. Uma única
chamada real de leitura concluiu: dois serviços de voo, 27 fatos e nenhum código
na fonte. O esquema estrito foi aceito pelo provedor.

A prévia e as remontagens foram feitas pelo worker separado. A versão integral
final tem três páginas, com horários em destaque, classe, bilhete, localizadores
distintos, contato, condições e trechos complementares da fonte. Foi conferida com
Poppler. A continuação e os ajustes de montagem usaram cache, com chave da IA vazia
no worker: **zero novas chamadas pagas**. Não houve seleção manual de dados nem
composição externa do PDF.

Uso informado pela leitura: 5.649 tokens de entrada e 5.038 de saída, incluindo
1.174 tokens de raciocínio nessa saída. Reserva estimada de US$ 0,10; isso não é
preço faturado. Sem busca paga, geração de imagens, reparo pago ou chamadas incertas.
O ledger permaneceu com uma chamada concluída e nenhum trabalho pendente.

O arquivo final continuou `needs_review`, sem aprovação humana. O botão desse
piloto é local, baixa a fonte correspondente por capacidade restrita e não libera
o premium; os hashes foram conferidos. Não recebeu a URL padrão do Bubble para
esse voucher. O contrato legado foi verificado nos testes sintéticos, sem mudar
o aplicativo Bubble.

Evidências privadas no workspace: `../../output/pdf/canonical-aereo-validado-20261030/`
e `var/canonical-implementation-20261030/real-pilot/button-validation.json`.
Uma amostra real não comprova generalização a todos os fornecedores nem preservação
semântica integral. Identidade/operadora, papel dos contatos e conteúdo que exista
somente em logos/imagens continuam parte da conferência humana.

Reprodução do ensaio sem custo de provedor:

```powershell
.venv/Scripts/python.exe scripts/smoke_canonical_http.py
```

Validação de um original real, com API/worker de homologação já iniciados:

```powershell
.venv/Scripts/python.exe scripts/validate_examples_http.py --source-dir <pasta-privada> --source-file <arquivo.pdf> --output-dir <pasta-nova> --base-url <origem-local> --require-render-mode html_template --require-pipeline-version canonical_v1 --generation-scope preview
```

O cliente recusa o pipeline errado antes do upload e salva o ID assim que a API
aceita, para que interrupções não levem a reenviar o arquivo e repetir cobrança.
Para lote deliberado há `--allow-batch --continue-on-failure`; nenhum bloqueio gera
repetição automática. A continuação integral da prévia usa o mesmo checkpoint.

## O que ainda precisa de homologação/evolução

Esta é a implementação inicial da arquitetura, não a conclusão das 18 seções do
plano nem uma garantia de interpretar qualquer arquivo. Permanecem:

1. Conjunto real de famílias/fornecedores não usados no ajuste, com referência
   humana para medir serviços, fatos, ressalvas e associações. Teste simulado não
   comprova a precisão do modelo; disponibilidade de rascunho não é aprovação.
2. Paginação/layout e extração física em tabelas, escaneamentos difíceis, idiomas,
   rotações, várias pessoas/códigos e documentos maiores do acervo. O OCR espacial
   inicial não é um motor completo de análise de tabelas ou de orientação.
3. A leitura atual usa uma chamada com teto de saída; recusa/incompletude geram
   pendência. Lotes com contexto para documentos extensos e reparo semântico pago
   opcional ainda não foram habilitados. Não há truncamento/repetição silenciosos.
4. Correções atuais exigem texto/evidência existentes. Transcrição humana baseada
   apenas em imagem ilegível para OCR e edição explícita de entidades/associações
   demandam evolução própria; casos assim continuam bloqueados para aprovação.
5. Trechos residuais são preservados literalmente, podendo repetir informações e
   manter o idioma da fonte. Refinar a concisão/reformulação somente com avaliação
   que comprove preservação material. As verificações literais não provam sozinhas
   o significado ou o papel de cada campo.
6. PostgreSQL real, Linux/Docker, carga/latência/custo, concorrência, backups,
   restauração, licenças, política de fotos e aceite operacional do cliente.
7. Configuração definitiva do novo serviço e integração Bubble separada, com URL
   legado correspondente ao voucher. Nenhuma publicação/migração foi realizada.

O modo anterior pode ser usado para seus documentos existentes. A ativação do
novo pipeline é explícita e reversível para novos uploads; não reescreve PDFs já
aprovados nem descarta checkpoints/custos anteriores.
