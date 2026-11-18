# Ativar as duas APIs no serviço existente

Decisão do usuário em 18/11/2026: usar o mesmo serviço do Dokploy, repositório e
domínio. Este procedimento substitui a orientação anterior de criar outro serviço.
O código não altera a configuração do servidor nem o Bubble automaticamente.

## 1. Registrar a configuração atual

Identificar o serviço que atende `/generate-pdf/`, o servidor de execução, o
diretório/comando de build, a porta pública, domínio e volumes existentes.
Guardar a configuração e o identificador da imagem atual de forma privada.
Confirmar memória/disco suficientes para as duas APIs, Chromium e um worker.

## 2. Conferir suporte do runtime ao sandbox

O Chromium deve executar como UID 10001, com sandbox habilitado. O container
precisa do perfil `premium/seccomp-chromium.json`, sem capabilities adicionais e
com `no-new-privileges`. Não usar `privileged`, `SYS_ADMIN`, seccomp unconfined ou
`--no-sandbox` para contornar uma falha.

Em Docker convencional, aplicar `security-opt seccomp=<perfil>` ao container.
Em serviço Swarm, o Docker Engine possui a configuração
`TaskTemplate.ContainerSpec.Privileges.Seccomp` (modo `custom`, perfil em base64).
O helper abaixo somente gera o JSON dessa configuração; não altera o servidor:

```bash
# Dentro de premium/, em cópia privada:
python3 scripts/shared_swarm_privileges.py --output /CAMINHO-PRIVADO/privileges.json
```

O responsável deve verificar se a versão instalada de Docker/Dokploy consegue
aplicar e preservar essa configuração por serviço em cada deploy. A interface
Swarm Settings não deve ser presumida como oferecendo esse campo. Uma alteração
avulsa que desapareça no próximo deploy não conclui esta etapa. Não mudar o perfil
global do daemon para resolver isso. Esse suporte no ambiente real ainda exige
verificação por quem tem acesso; os testes do repositório usam um container Docker
com o perfil explicitamente aplicado.

Referências: [Playwright Docker](https://playwright.dev/docs/docker),
[Docker Swarm ContainerSpec](https://github.com/moby/moby/blob/master/api/types/swarm/container.go)
e [Dokploy Advanced](https://docs.dokploy.com/docs/core/applications/advanced).

## 3. Ajustar o build do serviço atual

- Repositório: `devcubo3/api-pdf-difpass`; branch `main`.
- Método de build: Dockerfile.
- Contexto: raiz do repositório (`.`).
- Dockerfile: `premium/Dockerfile.shared`.
- Remover o override do comando antigo apenas nesta configuração do painel,
  deixando executar o CMD/ENTRYPOINT da nova imagem.
- Preservar o domínio atual e sua porta interna pública, normalmente `8000`.
  Configurar `PORT` com o mesmo valor. Portas internas `18101` e `18102` não são
  publicadas e não devem ser usadas como porta pública.

Os arquivos originais `api_pdf.py`, `requirements.txt`, `Procfile`, `index.py` e
`Dockerfile` continuam intactos para reversão. A imagem compartilhada usa Python
3.12 e dois ambientes virtuais, mantendo as versões declaradas no requirements
original separadas das versões da premium.

## 4. Configurar persistência e uma instância

Adicionar um volume nomeado exclusivo da premium ao MESMO serviço, montado
diretamente em `/app/var`, gravável por UID/GID `10001:10001`. Preservar os volumes
anteriores. O runtime verifica se esse diretório é um ponto de montagem.

Usar uma réplica, volume local em nó fixo e atualização/reversão `stop-first`.
Configurar tempo de parada de pelo menos 35 segundos. Um lock mantido pelo
processo impede premium simultânea no mesmo volume. O banco inicial é SQLite WAL
em `/app/var/state.sqlite3`; nenhum serviço PostgreSQL adicional é necessário.
Não usar NFS nem copiar um arquivo SQLite ativo isoladamente como backup.
Configurar backup consistente do banco pela API SQLite e dos arquivos do volume,
com retenção privada e teste de restauração.

## 5. Gerar e aplicar o ambiente

Em uma cópia privada, executar dentro de `premium/`:

```bash
python3 scripts/setup_env.py --shared-service
```

O comando cria `.env` novo sem sobrescrever um existente e sem imprimir tokens.
Ele já seleciona HTML, canonical_v1, SQLite e limites conservadores. Transferir os
valores ao Environment do serviço existente, preservando as variáveis antigas.
Preencher `OPENAI_API_KEY` e `PDF_PUBLIC_BASE_URL` com a chave privada e a origem
HTTPS da API atual, sem caminho adicional. Conferir `.env.shared.example`.

Valores essenciais: `PDF_PREMIUM_ENABLED=true`,
`PDF_DATA_DIR=/app/var`, `DATABASE_URL=sqlite:////app/var/state.sqlite3`,
`PDF_RENDER_MODE=html_template`, `PDF_PIPELINE_VERSION=canonical_v1` e o JSON real
de `PDF_API_TOKENS`. Os limites iniciais são US$ 0,15/documento e US$ 0,30/dia em
reservas estimadas, com reserva de busca de US$ 0,05 e limite de busca US$ 0,10/dia.
Esses valores não representam uma cobrança exata nem substituem conferir o
consumo real. Não instalar a chave ou o token no frontend do Bubble.

## 6. Publicar e verificar

Fazer deploy do serviço existente. Confirmar sua saúde e os processos legacy,
premium, worker e gateway nos logs. O healthcheck da imagem verifica ambas as
APIs e o heartbeat do worker, sem chamar IA. Remover healthcheck antigo do painel
que ignore o da imagem ou configurar o mesmo teste de `shared_service/health.py`.

No MESMO domínio atual, conferir:

- `/openapi.json`: título original e rota `/generate-pdf/`.
- `/premium/openapi.json`: schema premium com `/v2/documents`.
- `/healthz`: HTTP 200, `html_template` e `canonical_v1`.
- `/readyz`: HTTP 200 e `status=ready`.
- Rota premium autenticada sem token: HTTP 401.

Repetir o smoke legado em um cliente com dependências de teste instaladas:

```bash
# Da raiz do repositório:
python premium/scripts/smoke_legacy_http.py --base-url https://HOST-ATUAL
```

Confirmar um PDF sintético de duas páginas e o resultado `Legacy contract passed`.
Depois do primeiro restart/redeploy, verificar novamente saúde e persistência.
Não enviar automaticamente um lote de clientes: a homologação paga começa com
um único PDF, acompanhamento do registro de uso e revisão humana.

## 7. Entregar para integração com Bubble

Devolver URL atual, serviço/servidor, commit, resultado dos testes, configuração
persistente do sandbox e evidência do backup. Compartilhar tokens por canal
privado. A integração Bubble permanece separada: ela envia o original e
`legacy_pdf_url` com a URL do PDF padrão. Configurar hosts exatos por tenant em
`PDF_LEGACY_PDF_HOSTS` e exigir a URL antes de liberar a integração ao cliente.

## Reversão e comportamento em falhas

Com `PDF_PREMIUM_ENABLED=false`, a imagem compartilhada atende somente a API
antiga. Se a premium tiver configuração inválida, a API antiga continua acessível
e as rotas premium retornam 503; o healthcheck premium falha explicitamente.
O supervisor recupera processos interrompidos. Reiniciar o container inteiro
interrompe temporariamente ambas as APIs, uma consequência do serviço único.

Para reverter a publicação, restaurar o build/comando/imagem registrados na etapa
1 e manter o volume premium intacto. Preservar banco, artefatos e ledger de
chamadas concluídas/incertas. Não apagar volumes nem repetir chamadas pagas
incertas automaticamente. Confirmar novamente o smoke da API antiga.
