# Railway Deploy

Este projeto fica mais estavel no Railway porque o worker pode rodar como processo persistente, sem depender de cron externo para empurrar a fila.

## Arquitetura recomendada

- `web`: `python backend/server.py`
- `worker`: `python backend/worker_runner.py`
- banco: PostgreSQL gerenciado pelo Railway

No servico `web`, configure:

- `POSTHUB_INLINE_WORKER=0`
- `DATABASE_URL=${{Postgres.DATABASE_URL}}` ou a URL do PostgreSQL do projeto
- `ENCRYPTION_KEY_B64`, `JWT_SECRET`, `SESSION_SECRET`, `POSTHUB_ADMIN_*`
- `BASE_URL=https://web-production-66f10.up.railway.app`

No servico `worker`, configure as mesmas variaveis de aplicacao, inclusive `DATABASE_URL` e `ENCRYPTION_KEY_B64`.

## Passo a passo

1. Crie um novo projeto no Railway.
2. Adicione um banco PostgreSQL no mesmo projeto.
3. Crie um servico `web` apontando para este repositorio.
4. Em `Deploy > Start Command`, use `python backend/server.py`.
5. Crie um segundo servico `worker` apontando para o mesmo repositorio.
6. Em `Deploy > Start Command`, use `python backend/worker_runner.py`.
7. No servico `web`, defina `POSTHUB_INLINE_WORKER=0`.
8. Copie as mesmas variaveis essenciais para os dois servicos.
9. Acesse `/api/setup` uma vez apos o primeiro deploy para criar tabelas e semear o admin.

## Observacoes

- Se quiser usar SQLite no Railway por algum motivo, anexe um Volume e defina `POSTHUB_DATA_DIR` para o mount path. Mesmo assim, PostgreSQL continua sendo a opcao recomendada.
- Se voce importar o mesmo repositorio em mais de um servico, deixe o `Start Command` especifico de cada servico no painel do Railway.
- O `Dockerfile` deste repositorio ja deixa o deploy mais previsivel no Railway.
