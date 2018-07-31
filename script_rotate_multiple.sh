python rotate_proxies.py --stop

COUNTER=0
while [  $COUNTER -lt $1 ]; do
    python rotate_proxies.py --rotate
    let COUNTER=COUNTER+1
done

python rotate_proxies.py -W
python rotate_proxies.py

