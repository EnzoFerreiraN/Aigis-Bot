# Aigis 

Bot de Discord com player de música do YouTube e extrator de GIFs do X/Twitter.

## Funcionalidades

| Comando | Descrição |
|---|---|
| `!play <link ou busca>` / `/play` | Toca uma música ou adiciona à fila |
| `!skip` / `/skip` | Pula a faixa atual |
| `!queue` / `/queue` | Exibe a fila de reprodução |
| `!pause` / `/pause` | Pausa a reprodução |
| `!resume` / `/resume` | Retoma a reprodução pausada |
| `!stop` / `/stop` | Para a reprodução e sai do canal de voz |
| `!gif <link do X>` / `/gif` | Extrai e envia um GIF do X/Twitter |

> O prefixo padrão é `!` e pode ser alterado via a variável `COMMAND_PREFIX`.

---

## Arquitetura

```
┌─────────────────────────────────────────────────┐
│                  docker-compose                  │
│                                                  │
│  ┌──────────────────┐      ┌──────────────────┐  │
│  │   Serviço: bot   │      │ Serviço: cobalt  │  │
│  │                  │ HTTP │                  │  │
│  │  Python 3.12 +   │─────▶│  Instância       │  │
│  │  discord.py      │      │  self-hosted     │  │
│  │  + ffmpeg        │      │  ghcr.io/...     │  │
│  │  + yt-dlp        │      │  porta 9000      │  │
│  └──────────────────┘      └──────────────────┘  │
└─────────────────────────────────────────────────┘
```

- **`bot`** — Python + discord.py. Streaming de áudio via yt-dlp → ffmpeg (sem download). GIFs processados inteiramente em memória (sem disco).
- **`cobalt`** — Instância self-hosted do [Cobalt](https://github.com/imputnet/cobalt) para extração de GIFs do X/Twitter.

---

## Setup local (docker-compose)

### 1. Pré-requisitos

- [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/)
- Token do Discord

### 2. Configurar o bot no Developer Portal

1. Acesse o [Discord Developer Portal](https://discord.com/developers/applications) e crie uma aplicação.
2. Em **Bot > Privileged Gateway Intents**, ative:
   - ✅ **MESSAGE CONTENT INTENT**
   - ✅ **SERVER MEMBERS INTENT** (opcional)
3. Copie o **Token** do bot.
4. Em **OAuth2 > URL Generator**, selecione:
   - **Scopes:** `bot` + `applications.commands`
   - **Bot Permissions:** `Connect`, `Speak`, `Send Messages`, `Attach Files`, `Read Message History`
5. Use a URL gerada para convidar o bot ao seu servidor.

### 3. Variáveis de ambiente

```bash
cp .env.example .env
```

Edite `.env`:

```env
DISCORD_TOKEN=seu_token_aqui
COMMAND_PREFIX=!
COBALT_API_URL=http://cobalt:9000
```

> `COBALT_API_URL` já está configurado como `http://cobalt:9000` no `docker-compose.yml`.
> Só precisará alterar se rodar os serviços separadamente.

### 4. Subir os serviços

```bash
docker-compose up --build
```

Na primeira execução o Docker irá:
- Baixar a imagem do Cobalt (`ghcr.io/imputnet/cobalt:11`)
- Construir a imagem do bot (Python + ffmpeg + dependências)

Para rodar em background:

```bash
docker-compose up --build -d
docker-compose logs -f bot  # acompanhar logs do bot
```

Para parar:

```bash
docker-compose down
```

---

## Deploy no Railway

> O Railway **não usa `docker-compose`**. Cada serviço vira um serviço independente no projeto.

### Passos

1. **Crie um projeto** no [Railway](https://railway.app).

2. **Serviço `bot`** — deploy a partir deste repositório:
   - *Source:* seu repositório GitHub
   - *Root Directory:* `bot/`
   - O Railway detecta o `Dockerfile` automaticamente.
   - Variáveis de ambiente a configurar no serviço:

     | Variável | Valor |
     |---|---|
     | `DISCORD_TOKEN` | seu token do Discord |
     | `COMMAND_PREFIX` | `!` |
     | `COBALT_API_URL` | `http://cobalt.railway.internal:9000` |

3. **Serviço `cobalt`** — deploy a partir de imagem Docker:
   - *Source:* Docker Image → `ghcr.io/imputnet/cobalt:11`
   - Variáveis de ambiente:

     | Variável | Valor |
     |---|---|
     | `API_URL` | `http://cobalt.railway.internal:9000/` |
     | `API_PORT` | `9000` |
     | `API_LISTEN_ADDRESS` | `::` *(IPv6 — obrigatório na rede privada do Railway)* |

### Rede privada do Railway

A rede privada do Railway é **IPv6-only**. Por isso:
- `API_LISTEN_ADDRESS=::` faz o Cobalt escutar em todas as interfaces IPv6.
- O bot acessa via `http://cobalt.railway.internal:9000` (domínio interno).
- O serviço `cobalt` **não precisa de domínio público** — é alcançado apenas internamente.

### Deploy contínuo (CI/CD)

Configure o serviço `bot` para fazer **auto-deploy** a cada push na branch `main` nas configurações do serviço no Railway. O Railway rebuilda a imagem automaticamente.

> **Tip:** Use a [skill do Railway no Claude Code](https://railway.app) (`/use-railway`) para provisionar serviços, configurar variáveis e gerenciar o deploy diretamente pelo terminal.

---

## Notas técnicas

- **Zero disco:** nenhum arquivo temporário é criado. Áudio via streaming yt-dlp→ffmpeg; GIFs via `io.BytesIO`.
- **URLs do YouTube expiram:** a URL de stream é resolvida no momento de tocar, não quando entra na fila, para evitar erros 403.
- **Inatividade:** após 20 minutos sem reprodução, o bot desconecta do canal de voz e libera a fila da memória.
- **GIFs grandes:** se o GIF exceder 8 MB, o bot tenta recomprimir via ffmpeg (pipe→pipe) antes de enviar. Se ainda exceder, avisa o usuário.
- **yt-dlp:** instalado via pip para facilitar atualizações. Para atualizar: rebuild da imagem (`docker-compose up --build`).
