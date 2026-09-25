import QtQuick

Canvas {
    id: root
    property color accent: "#70dfff"
    property bool perspective: false
    antialiasing: true
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onAccentChanged: requestPaint()
    onPerspectiveChanged: requestPaint()
    onPaint: {
        const c = getContext("2d"); c.reset()
        const glow = c.createRadialGradient(width*.55,height*.4,0,width*.55,height*.4,width*.65)
        glow.addColorStop(0,"#102d42"); glow.addColorStop(1,"#050b13")
        c.fillStyle=glow; c.fillRect(0,0,width,height)
        c.strokeStyle=root.accent; c.lineWidth=.6; c.globalAlpha=.065
        for(let x=0;x<width;x+=48) { c.beginPath(); c.moveTo(x,0); c.lineTo(x,height); c.stroke() }
        for(let y=0;y<height;y+=48) { c.beginPath(); c.moveTo(0,y); c.lineTo(width,y); c.stroke() }
        c.globalAlpha=.25
        for(let x=24;x<width;x+=144) for(let y=24;y<height;y+=144) {
            c.beginPath(); c.moveTo(x-3,y); c.lineTo(x+3,y); c.moveTo(x,y-3); c.lineTo(x,y+3); c.stroke()
        }
        if(root.perspective) {
            c.globalAlpha=.12
            for(let i=-10;i<=10;++i) { c.beginPath(); c.moveTo(width*.5+i*25,height*.66); c.lineTo(width*.5+i*150,height); c.stroke() }
            for(let i=0;i<9;++i) { const y=height*.66+Math.pow(i/8,2)*height*.34; c.beginPath(); c.moveTo(0,y); c.lineTo(width,y); c.stroke() }
        }
    }
}
