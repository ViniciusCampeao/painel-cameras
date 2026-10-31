# Face Detection + YOLO Integration

## O que foi adicionado

Um novo endpoint `/frame/<cam_id>/detect` que processa o feed de cada câmera em tempo real com:
- **YOLO8n**: detecção de objetos (pessoas, carros, animais, etc)
- **face_recognition**: identificação de rostos conhecidos vs desconhecidos

## Como usar

1. **Iniciar o painel** normalmente:
   ```bash
   cd /home/vinicius/painel-cameras
   source venv/bin/activate
   python painel_cameras.py
   ```

2. **No navegador**, acessar `http://localhost:5055` (ou IP da VPS)

3. **Por câmera**, clicar no botão **🔍 (lupa)** para ativar/desativar a detecção

   - Quando ativo: mostra **retângulos coloridos** ao redor de objetos detectados
   - Verde: rostos conhecidos (da pasta `~/vigilancia/known_faces/`)
   - Vermelho: rostos desconhecidos
   - Laranja: objetos (carros, etc)
   - Azul: pessoas

## Monitoramento de CPU/RAM

Para medir o impacto real enquanto testa, abra outro terminal:

```bash
watch -n 1 'ps aux | grep painel_cameras; free -h | head -2'
```

Ou use `htop`:
```bash
htop -p $(pgrep -f "python painel_cameras.py")
```

## Configuração

Edite `detector_lite.py` para ajustar:

```python
# Frequência de detecção (a cada N frames)
# Quanto maior, menos CPU, mas detecção mais lenta
if self.frame_count % 3 == 0:  # linha ~157
    self._detect(frame)

# Limiar de confiança YOLO
conf=0.45

# Folder de rostos conhecidos
self.face_mgr = FaceManager("/home/vinicius/vigilancia/known_faces")
```

## Performance esperada

Com 2 câmeras e detecção ativa:
- **CPU**: ~30-50% de um core (após otimizações)
- **RAM**: +500-800MB

Se ficar muito pesado, aumente `DETECT_INTERVAL` ou desative a detecção.

## Próximas otimizações

- [ ] Motion gating: só rodar YOLO quando houver movimento
- [ ] Inference em GPU (se disponível)
- [ ] Cache compartilhado de modelo entre câmeras
- [ ] Webhook para alertas (ao detectar pessoa desconhecida, etc)
