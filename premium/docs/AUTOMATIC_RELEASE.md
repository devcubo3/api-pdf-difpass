# Entrega automática validada

`PDF_REVIEW_POLICY=always` preserva a revisão manual existente. A opção
`validated_auto` permite disponibilizar o premium sem uma decisão humana no fluxo
completo `html_template` + `canonical_v1`. Não muda `/generate-pdf/`.

## Contrato para o Bubble

1. Preservar o original do fornecedor. Salvar o retorno da API antiga no Bubble
   como **PDF de segurança** e obter sua URL CDN persistente.
2. Enviar os bytes do original em `input_pdf` e essa URL em `legacy_pdf_url`, com
   uma chave `Idempotency-Key` estável por upload. O hostname HTTPS precisa estar
   em `PDF_LEGACY_PDF_HOSTS` para a organização autenticada. A chave da API fica
   no conector privado; não vai para o navegador, arquivos ou registros públicos.
3. Consultar `GET /v2/documents/{id}`. A criação retorna JSON 202, não um PDF.
4. Quando `status=ready`, obter `GET /v2/documents/{id}/pdf` autenticado e salvar
   efetivamente esse File no armazenamento Bubble antes de trocar `arquivo`.
5. Compartilhar esse mesmo processamento entre os vouchers do envio para todos
   os passageiros. Não chamar a IA novamente ao copiar o arquivo ou consultar.

O botão no documento final se chama **Baixar PDF de segurança** quando aponta
para `legacy_pdf_url`. A URL nunca é buscada pelo servidor premium; apenas passa
por validação de origem e é incluída no PDF. A acessibilidade e a persistência
do arquivo no Bubble precisam ser confirmadas na integração.

## Critérios para `ready`

O worker exige evidências canônicas válidas, cobertura dos blocos originais,
texto e layout de todas as páginas, cabeçalhos verificados, códigos preservados
e associados, ausência de problemas bloqueantes ou chamadas incertas na revisão,
hash do original e do PDF final conferidos, nenhum anexo e exatamente um link
para o PDF de segurança esperado. Prévias nunca são liberadas automaticamente.

A publicação da revisão e o evento `automatic_release`, com sujeito
`system:validated-auto`, são gravados na mesma transação com controle de
concorrência. Uma tentativa obsoleta não pode publicar sobre uma revisão nova.

A política é capturada no envio. Trabalhos antigos sem essa captura conservam
`always`; alterar a variável não aprova o histórico. O worker também precisa
estar em `validated_auto`: voltar a `always` interrompe novas liberações
automáticas sem revogar PDFs já entregues. Não existe aprovação humana simulada.

## Fotos

Fotos descobertas na web continuam sujeitas à confirmação existente. O fluxo
automático usa apenas uma seleção já confirmada pela equipe ou um asset privado
previamente autorizado em `PDF_MEDIA_AUTO_CATALOG`. Essa configuração é um mapa
por organização com entradas `{asset_id, name, locality}`; `asset_id` é o hash
de 64 caracteres do catálogo privado. Nome e localidade devem corresponder
exatamente ao lugar comprovado no documento, sem busca aproximada.

Sem correspondência apta, o cabeçalho usa arte institucional Diff Travel e
registra `MEDIA_INSTITUTIONAL`, sem fingir ser foto do hotel ou atração e sem
chamada paga de busca. A arte não muda a leitura dos dados nem gera uma imagem
de IA por página. Fotos de outra organização ou arquivos adulterados são recusados.

## Falhas e recuperação

Falhas continuam em `needs_review`/`failed`, com download premium bloqueado.
O Bubble pode oferecer à equipe a liberação explícita da segurança provisória,
registrando responsável, motivo e data. Isso não é uma decisão de aprovação da
premium e não deve chamar `/review/decision` automaticamente.

Falha ao copiar para o Bubble exige repetir só download/salvamento. Timeout de
criação exige recuperar a mesma chave e conteúdo, não criar outro processamento.
Uma substituição conserva o principal vigente e só o troca se o processamento
e a revisão ainda forem os atuais. Não reprocessar históricos em lote.

## Evidência de testes

`tests/test_auto_release.py` cobre a entrega por HTTP, idempotência, prévia,
política histórica/manual, vínculos de segurança, integridade e catálogo por
organização/lugar. `scripts/smoke_canonical_http.py --review-policy validated_auto`
exercita API e worker separados com um fornecedor sintético local: zero chamadas
pagas. A variante compartilhada verifica também o endpoint antigo e a recuperação
do worker/serviço. Esses testes não substituem a homologação dos workflows e da
persistência de arquivos dentro do Bubble.
