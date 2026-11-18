# Publicação da API premium

O repositório é compartilhado; os processos são independentes.

| Serviço | Diretório de build | Inicialização | Rota de entrada |
| --- | --- | --- | --- |
| API existente | raiz | `api_pdf:app`, Python 3.11 | `POST /generate-pdf/` |
| API premium | `premium/` | `app.main:app`, Python 3.12 | `POST /v2/documents` |
| Worker premium | `premium/` | `python -m app.worker` | sem porta pública |

Não trocar o diretório, o comando de início, o domínio, o banco nem os volumes do
serviço existente. O Bubble continua chamando o endpoint atual até a integração
premium ser configurada e homologada separadamente.

## Configuração no Dokploy

1. Identificar o serviço existente e o servidor onde ele executa. O endereço do
   painel pode ser diferente do endereço do servidor de execução. Conferir memória,
   disco e portas disponíveis antes de criar os novos containers.
2. Criar um serviço **Docker Compose** premium com este repositório, branch `main`,
   arquivo `premium/docker-compose.yml`. Aplicar também
   `premium/docker-compose.dokploy.yml` para conectar somente a API à rede do proxy.
   Se a interface exigir um comando personalizado, preservar o nome de projeto
   exclusivo gerado pelo Dokploy e usar os dois arquivos com `-f`.
3. Gerar credenciais exclusivas em uma pasta privada com
   `python scripts/setup_env.py --docker`, executado dentro de `premium/`.
   Configurar os valores no editor Environment do serviço premium, sem colocá-los
   no repositório ou em logs. Aplicar o perfil de `.env.dokploy.example` sobre os
   valores gerados. A chave OpenAI é configurada nesse ambiente privado.
4. Conferir `PDF_RENDER_MODE=html_template`, `PDF_PIPELINE_VERSION=canonical_v1`,
   revisão obrigatória e limites de gasto. Confirmar uma porta de loopback livre
   em `PDF_HOST_PORT`. Manter `db` e `worker` sem portas públicas.
5. Criar um domínio HTTPS próprio apontando para `api`, porta interna `8000`.
   Usar esse endereço em `PDF_PUBLIC_BASE_URL` antes de emitir links de download.
6. Fazer o build e subir `api`, `worker` e `db`. Conferir os três serviços e seus
   healthchecks. Chromium deve executar com sandbox e usuário sem privilégios.
   Uma falha de sandbox exige verificar o suporte do host, preservando as
   proteções do container.

O arquivo seccomp é lido na criação do container; mantê-lo disponível no checkout
de deploy. Os dados de documentos e PostgreSQL usam volumes nomeados próprios.
Configurar backup desses dois volumes com retenção e teste de restauração.

Referências oficiais: [Docker Compose no Dokploy](https://docs.dokploy.com/docs/core/docker-compose)
e [Chromium em containers](https://playwright.dev/docs/docker).

## Verificação após publicar

- Confirmar HTTP 200 em `/healthz` e `/readyz` do domínio premium, além do heartbeat
  recente do worker. Verificar que `/openapi.json` contém `/v2/documents`.
- Repetir `python premium/scripts/smoke_legacy_http.py --base-url <origem-antiga>`
  em um cliente com as dependências de teste instaladas. Ele envia somente um
  documento sintético de duas páginas e valida a resposta PDF.
- Confirmar que a configuração do endpoint antigo no Bubble continua igual.
- Antes do uso com clientes, homologar upload, processamento, revisão, aprovação,
  download e botão do PDF padrão da API antiga. Esse ensaio real pode chamar IA;
  acompanhar o orçamento e o registro de uso antes de repetir qualquer geração.

O workflow do repositório testa o pipeline com dados sintéticos, PostgreSQL,
cliente HTTP e worker separado, a imagem Docker e o contrato da API antiga.
Esses testes não usam chaves reais nem consomem créditos de IA.

## Integração posterior com o Bubble

O Bubble deve conservar os bytes do fornecedor e a URL do PDF padrão já gerado.
A nova chamada envia o original para `/v2/documents` com `legacy_pdf_url` apontando
para esse PDF padrão. Configurar os hosts exatos permitidos por organização em
`PDF_LEGACY_PDF_HOSTS`; depois ativar `PDF_REQUIRE_LEGACY_PDF_URL=true`.
Tokens ficam exclusivamente no backend do Bubble. O retorno inicial é assíncrono:
é necessário consultar o processamento e concluir a revisão antes da entrega.

## Reversão

Se a premium falhar, parar somente seu serviço Compose. Preservar os volumes e os
registros de chamadas concluídas ou incertas, evitando cobrança duplicada em uma
retomada. Reimplantar uma versão premium validada pelo seu commit. Não executar
`down --volumes`, não limpar o armazenamento e não alterar a API existente para
resolver uma falha premium. Repetir as verificações de saúde e o smoke legado.
