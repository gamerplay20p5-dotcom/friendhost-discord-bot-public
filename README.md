# FriendHost Discord Bot

Bot administrativo da FriendHost para Discord, integrado ao Supabase.

## Funcoes

- `/central`: apresenta os comandos administrativos em uma resposta privada.
- `/pedidos_recentes`: consulta os pedidos recentes do site.
- `/faturas`: consulta o historico de faturas de um cliente.
- `/gerar_fatura`: cria uma fatura manual no Supabase.
- `/assistente`: assistente interno com Gemini, opcional e sem acesso ao Supabase.
- `/atualizar_bot`: atualiza o bot pelo GitHub e reinicia o processo.
- `/catalogo`, `/alterar_plano` e `/plano_status`: consultam, atualizam e pausam planos publicados.
- `/cupons`, `/criar_cupom` e `/cupom_status`: administram cupons usados pelo checkout do site.
- Ponte de suporte: mensagens enviadas pelo site em `mensagens_suporte` criam/atualizam canais privados `atendimento-*` no Discord.
- Alertas opcionais de novos pedidos em um canal privado da equipe.
- Honeypot anti-spam: mensagens de usuarios comuns no canal configurado sao removidas e a conta e expulsa automaticamente.
- Respostas digitadas nos canais de atendimento voltam para o Supabase e aparecem no site.

## Configuracao

1. Crie um ambiente virtual:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Instale as dependencias:

```powershell
pip install -r requirements.txt
```

3. Copie `.env.example` para `.env` e preencha os valores reais.

4. Inicie o bot:

```powershell
python main.py
```

## Variaveis de ambiente

- `DISCORD_TOKEN`: token do bot no Discord.
- `GUILD_ID`: ID do servidor Discord onde os comandos serao registrados.
- `SUPPORT_CATEGORY_ID`: ID da categoria onde canais de atendimento serao criados.
- `SUPABASE_URL`: URL do projeto Supabase.
- `SUPABASE_SERVICE_KEY`: service role key do Supabase. Nunca envie isso para o GitHub.
- `BOT_NAME`: nome usado em logs e embeds.
- `SUPPORT_POLL_SECONDS`: intervalo da ponte de suporte.
- `SUPPORT_SYNC_ON_START`: use `true` para enviar mensagens antigas ainda nao processadas ao ligar.
- `STAFF_ROLE_IDS`: IDs de cargos internos separados por virgula. Sem eles, somente administradores do Discord podem consultar ou alterar dados.
- `ORDERS_CHANNEL_ID`: ID de um canal privado da equipe para alertas de novos pedidos. Deixe vazio para desativar os alertas.
- `ORDERS_POLL_SECONDS`: intervalo de verificacao de pedidos novos.
- `ORDERS_SYNC_ON_START`: use `true` somente se quiser reenviar pedidos antigos quando o bot iniciar.
- `HONEYPOT_CHANNEL_ID`: canal usado como honeypot anti-spam. O bot publica o aviso automaticamente e expulsa usuarios comuns que enviarem mensagens nele.
- `GEMINI_API_KEY`: chave do Google AI Studio para habilitar `/assistente`. Nunca publique esta chave.
- `GEMINI_MODEL`: modelo Gemini utilizado, por padrao `gemini-2.5-flash`.
- `AUTO_UPDATE_ENABLED`: habilita ou bloqueia o comando `/atualizar_bot`.
- `AUTO_UPDATE_REMOTE`: remote Git usado no update. Normalmente `origin`.
- `AUTO_UPDATE_BRANCH`: branch usada no update. Se ficar vazio, usa a branch atual.

## Auto-update

Os comandos que acessam dados internos sao restritos a administradores do Discord ou aos cargos configurados em `STAFF_ROLE_IDS`. As respostas de financeiro, suporte e IA sao efemeras: apenas quem executou o comando consegue ve-las.

Fluxo aplicado:

```text
git fetch
git pull --ff-only
pip install -r requirements.txt
restart do processo Python
```

Ele nao executa comandos enviados pelo Discord. A atualizacao e sempre feita a partir do remote/branch configurado no `.env`.

Se existirem arquivos modificados no servidor, o update e bloqueado para evitar sobrescrever mudancas locais.

## Catalogo e cupons

Antes de usar os comandos de catalogo ou cupons, execute `supabase/catalogo.sql` no SQL Editor
do projeto Supabase. O arquivo cria a tabela de catalogo, os controles de resgate de cupom e
as funcoes usadas exclusivamente pelo backend.

Os valores exibidos no site sao consultados do catalogo a cada carregamento da pagina. Para
alterar um plano, consulte o SKU com `/catalogo` e use `/alterar_plano`. O bot altera apenas
preco de primeiro mes, renovacao, rotulo promocional e disponibilidade; recursos como RAM e
CPU permanecem protegidos pela migracao e pelo checkout no servidor.

Cupons sao validados no backend durante a criacao do pedido. O front-end nunca define desconto,
preco ou disponibilidade final. Use `/criar_cupom` com tipo `percentual` ou `fixo`, e opcionalmente
limite de usos, validade em horas e `plano_sku` para restringir a campanha a um plano especifico,
por exemplo `ddr4-8`. Deixe `plano_sku` vazio para permitir todos os planos.

Cupons de 100% geram um pedido gratuito e confirmado, sem criar checkout no Mercado Pago. Para
operacao diaria, use `/editar_cupom` para ajustar valores, limites, validade ou o SKU permitido;
use `/desativar_cupom` para pausar uma campanha imediatamente.

## Seguranca

O arquivo `.env` fica ignorado pelo Git. Use somente `.env.example` como modelo publico.

Use a `SUPABASE_SERVICE_KEY` apenas no servidor do bot. Ela ignora RLS e nao deve aparecer no front-end, no Vercel client-side ou em prints.

O canal configurado em `ORDERS_CHANNEL_ID` deve ser privado e visivel apenas para a equipe. O bot envia e-mails e dados de pedidos nesse canal para agilizar o atendimento.

Para o honeypot funcionar, conceda ao bot as permissoes `Expulsar membros`, `Gerenciar mensagens`, `Ler historico de mensagens` e `Enviar mensagens`. O cargo do bot precisa ficar acima dos cargos dos usuarios que ele podera expulsar.

A IA nao recebe automaticamente faturas, pedidos, e-mails, numeros de telefone ou conteudo do Supabase. Nao cole informacoes pessoais, credenciais ou dados de pagamento no comando `/assistente`.
