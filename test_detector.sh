#!/bin/bash
# Script para testar face detection com monitoramento de recursos

set -e

cd "$(dirname "$0")"
source venv/bin/activate

echo "======================================================"
echo "  Face Detection + YOLO Benchmark"
echo "  CPU cores: $(nproc), RAM: $(free -h | awk 'NR==2 {print $2}')"
echo "======================================================"
echo

# Inicia o painel em background
echo "[*] Iniciando painel-cameras..."
python painel_cameras.py &
PAINEL_PID=$!
echo "    PID: $PAINEL_PID"
sleep 5

echo
echo "[*] Painel rodando. Monitore em outro terminal:"
echo
echo "    watch -n 1 'ps aux | grep $PAINEL_PID | grep -v grep; free -h | head -2'"
echo
echo "    ou"
echo
echo "    htop -p $PAINEL_PID"
echo
echo "[*] Acesse: http://localhost:5055"
echo "[*] Clique no botão 🔍 para ativar detecção facial"
echo
echo "Pressione Ctrl+C para parar..."
echo

# Monitora o processo
while kill -0 $PAINEL_PID 2>/dev/null; do
    sleep 60
    ps aux | grep $PAINEL_PID | grep -v grep
done

trap "kill $PAINEL_PID 2>/dev/null; exit 0" INT TERM

wait $PAINEL_PID
