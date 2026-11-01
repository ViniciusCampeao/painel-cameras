# Painel de Câmeras

Painel web para visualizar e controlar câmeras IP ONVIF na rede local, sem depender do
aplicativo do fabricante nem de plugins no navegador. Um Flask serve as imagens via MJPEG,
áudio via MP3 e controle PTZ via ONVIF, tudo atrás de autenticação básica.

Projeto pessoal, feito pra rodar na própria rede de casa. Não foi pensado pra multi-usuário
nem pra escalar além de um punhado de câmeras.

## Como funciona

Cada câmera fala RTSP (vídeo/áudio) e ONVIF (descoberta de perfil e controle PTZ) na porta
que o fabricante configurou. O navegador não consegue reproduzir RTSP/H.264 diretamente, então
o servidor usa `ffmpeg` pra reempacotar o stream em algo que HTML entende:

- Vídeo: RTSP → MJPEG, servido como `multipart/x-mixed-replace` (o clássico truque de stream
  de imagens que qualquer `<img>` consegue exibir sem JS especial).
- Áudio: RTSP → MP3, servido como stream contínuo pra uma tag `<audio>`.
- Snapshot: um único frame extraído sob demanda pra thumbnail/preview.

O `ffmpeg` só roda enquanto tem gente vendo aquela câmera especificamente — sem viewer, o
processo é encerrado. Isso evita consumir CPU com transcodificação de câmera que ninguém está
olhando.

## Requisitos

- Python 3.10+
- `ffmpeg` instalado no sistema (`apt install ffmpeg`)
- Câmeras ONVIF acessíveis na mesma rede

## Instalação

```bash
git clone <url-do-repo>
cd painel-cameras
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edite o `.env` com os dados de cada câmera e a senha de acesso ao painel (veja a seção
seguinte pra descobrir IP, usuário e senha).

Rodar localmente:

```bash
venv/bin/python painel_cameras.py
```

Sobe em `127.0.0.1:5055`. Não expõe pra internet sozinho — é assim de propósito, ver seção
de Segurança no final.

## Descobrindo o IP da câmera

Três jeitos, do mais fácil ao mais manual:

1. **App oficial da câmera** (o mesmo que você usou pra configurar ela pelo celular). Na
   maioria dos apps de câmera chinesa (v380, CamHi, Yoosee, etc.) o IP atual aparece em
   Configurações do Dispositivo → Informações de Rede, ou similar. É o caminho mais confiável
   porque mostra o IP que a câmera está usando naquele momento.
2. **Lista de clientes do roteador**: entra na interface do roteador (geralmente
   `192.168.0.1` ou `192.168.1.1`) e procura a lista de dispositivos conectados / DHCP leases.
   A câmera costuma aparecer com um hostname que dá pra reconhecer pelo fabricante.
3. **nmap**, quando os dois acima não são uma opção (câmera sem app, ou você só quer confirmar
   por fora):

```bash
# descobre todos os hosts ativos na rede
nmap -sn 192.168.3.0/24
```

Compare o resultado com o que já está na rede (celular, notebook, etc.) por eliminação, ou
procure fabricantes conhecidos de câmera no campo MAC vendor que o nmap mostra.

## Usuário e senha

É o mesmo usuário/senha cadastrado no app oficial da câmera — não tem usuário separado pra
RTSP/ONVIF na grande maioria dos modelos chineses genéricos. Normalmente vem com um usuário
`admin` e senha padrão de fábrica (`admin`, `12345`, `123456` dependendo do lote), que o app
pede pra trocar no primeiro acesso. Se você trocou a senha pelo app, é essa mesma senha que
vai no `.env`.

Se a câmera não aceitar a conexão RTSP mesmo com a senha certa, vale checar no app se existe
uma opção separada tipo "senha de stream" ou "senha ONVIF" — alguns fabricantes (Dahua/Hikvision
e clones) têm isso escondido em configurações avançadas.

## Localizando portas com nmap

Cada fabricante usa uma porta ONVIF diferente pra descoberta/controle, então esse projeto tenta
uma lista de portas conhecidas (`PORTAS_ONVIF` no código) quando a porta não é informada. Pra
descobrir manualmente quais portas uma câmera específica tem abertas:

```bash
nmap -p 554,80,8000,2020,5000,8080,8899,8081,10080,50000 192.168.3.29
```

- `554` é a porta padrão de RTSP (vídeo/áudio) — quase universal.
- As demais são portas de serviço ONVIF/HTTP que variam por fabricante. Se nenhuma dessas
  responder, um scan mais largo ajuda a achar a porta certa:

```bash
nmap -p 1-65535 --open 192.168.3.29
```

Portas abertas com serviços tipo `http` ou desconhecidos na faixa 8000-10000 geralmente são o
ONVIF da câmera.

## Tipos de saída de vídeo

Câmera ONVIF normalmente expõe pelo menos dois perfis de stream:

- **Main stream** (`/onvif1` na maioria dos modelos usados aqui): resolução alta, bitrate
  maior, é o que a câmera grava. Pesado pra transcodificar em tempo real com poucos recursos.
- **Sub stream** (geralmente `/onvif2`): resolução menor, pensado justamente pra visualização
  remota/app, mais leve de decodificar e transcodificar.

Esse painel usa o path configurado em cada câmera (campo `path` no `.env`/config) sem
diferenciar os dois — pra câmeras com CPU do servidor limitada, vale apontar pro sub stream
em vez do main.

Além do vídeo, tem o **stream de áudio**, que em algumas câmeras vem embutido no mesmo path
RTSP do vídeo e em outras é um path separado (campo `audio_path`, usado quando o áudio não
vem junto).

## Transcoding

O vídeo é convertido assim (ver `StreamManager._cmd` no código):

```bash
ffmpeg -rtsp_transport tcp -i rtsp://user:senha@host:554/path \
  -an -vf scale=640:-1 -r 8 -q:v 8 -f image2pipe -vcodec mjpeg -
```

- `-an` descarta áudio (vai por outro pipeline).
- `-vf scale=640:-1` reduz a largura pra 640px mantendo proporção — suficiente pra
  visualização em painel, sem gastar CPU decodificando/reescalando em resolução cheia.
- `-r 8` limita a 8 quadros por segundo. MJPEG num painel de monitoramento não precisa de
  30fps, e isso corta bastante o custo de CPU por câmera simultânea.
- `-q:v 8` é a qualidade JPEG de cada frame (escala do ffmpeg, menor é melhor qualidade).

O áudio segue separado:

```bash
ffmpeg -fflags nobuffer -rtsp_transport tcp -i rtsp://user:senha@host:554/path \
  -vn -acodec libmp3lame -b:a 32k -ac 1 -ar 16000 -flush_packets 1 -f mp3 -
```

Mono, 16kHz, 32kbps — dá pra ouvir se tem alguém falando ou barulho, não é pra qualidade de
música. `-flush_packets 1` evita que o ffmpeg segure buffer antes de mandar os pacotes, o que
deixaria o áudio com atraso perceptível em relação ao vídeo.

Se o hardware aguentar mais qualidade, dá pra subir `-r`, tirar o `scale` ou trocar `mjpeg`
por outro codec — só lembrando que MJPEG puro tende a pesar mais de banda que H.264 por causa
de escolher robustez/compatibilidade acima de eficiência de compressão.

## PTZ

Controle de pan/tilt/zoom via ONVIF (`ContinuousMove`), com auto-detecção de porta usando a
mesma lista do nmap acima. Cada câmera pode inverter pan/tilt individualmente (`inverter_pan`,
`inverter_tilt`) pra corrigir instalação física invertida, sem precisar mexer no firmware da
câmera.

## Rodando como serviço (systemd)

```ini
[Unit]
Description=Painel de Cameras
After=network.target

[Service]
User=seu_usuario
WorkingDirectory=/caminho/para/painel-cameras
ExecStart=/caminho/para/painel-cameras/venv/bin/python painel_cameras.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

O painel tem um botão de reiniciar o próprio serviço, que depende de uma regra de sudo restrita
a esse comando específico:

```
seu_usuario ALL=(root) NOPASSWD: /usr/bin/systemctl restart --no-block painel-cameras.service
```

Sem essa linha em `/etc/sudoers.d/`, o botão de restart no painel não funciona (o resto do
painel funciona normal).

## Gravação e registro

Cada câmera grava continuamente em arquivos de 5 minutos (`SEGMENTO_S`), e as gravações
ficam guardadas por 24 h (`RETENCAO_HORAS`). Depois disso, o próprio painel apaga.

- **Cozinha**: grava o stream HEVC em cópia (quase zero de CPU). Toca nativamente em
  Safari/iPhone; Chrome em Linux pode não tocar H.265.
- **Garagem**: o stream UDP não grava em cópia, então é recodificado para H.264 640×360
  (custo baixo de CPU). Toca em qualquer navegador.

Abas do painel:

- **Ao vivo**: o painel de sempre.
- **Registros**: pessoas detectadas (YOLO, classe `person`), com miniatura e horário.
  Um evento começa na primeira detecção e fecha depois de `REGISTRO_GAP_S` sem ver ninguém.
  O registro fica salvo mesmo depois que o vídeo é apagado.
- **Gravações**: player com barra de tempo sobre toda a retenção. Clicar num registro abre
  o player no horário do evento; o campo "Ir para o horário" e os botões de ±1/±10 min
  permitem andar livremente.

Os arquivos ficam em `GRAVACAO_DIR` (padrão `~/gravacoes`, fora do repositório). Para mover
para um HD externo, monte o disco, aponte `GRAVACAO_DIR` pra ele e reinicie o serviço, ou
copie as pastas `cozinha/`, `garagem/` e `registros/` antes de 24 h passarem.

## Segurança

- O `.env` nunca deve ir pro repositório — está no `.gitignore` por isso. Use o `.env.example`
  como referência de quais variáveis preencher.
- Câmera genérica costuma vir com senha padrão de fábrica. Troque antes de expor qualquer
  coisa relacionada a ela, inclusive esse painel.
- O Flask sobe só em `127.0.0.1` de propósito — não é servidor de produção e não tem
  rate limiting. Pra acessar de fora de casa, coloque atrás de um proxy/túnel que já resolva
  TLS e autenticação (Cloudflare Tunnel, nginx com basic auth, etc.), não abra a porta 5055
  direto no roteador.
