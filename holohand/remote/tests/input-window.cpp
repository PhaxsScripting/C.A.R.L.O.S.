#include <QApplication>
#include <QScreen>
#include <QWindow>
#include <QWidget>
#include <QVBoxLayout>
#include <QLabel>
#include <QTextEdit>
#include <QPushButton>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMouseEvent>
#include <QWheelEvent>
#include <QKeyEvent>
#include <QTimer>
class TestWindow: public QWidget {
public:
 QTextEdit *editor;QJsonObject receipt;
 TestWindow(){
  setWindowTitle("HoloHand input acceptance test");resize(600,380);
  auto *layout=new QVBoxLayout(this);layout->addWidget(new QLabel("HOLOHAND INPUT TEST\nThis temporary window receives remote input only for verification."));
  editor=new QTextEdit;editor->setPlaceholderText("Tap here and type HOLOHAND INPUT OK");layout->addWidget(editor);
  auto *button=new QPushButton("Test click");layout->addWidget(button);
  connect(button,&QPushButton::clicked,this,[this]{receipt["button_clicked"]=true;save();});
  connect(editor,&QTextEdit::textChanged,this,[this]{receipt["text_matches"]=editor->toPlainText().contains("HOLOHAND INPUT OK");save();});
  editor->installEventFilter(this);editor->viewport()->installEventFilter(this);button->installEventFilter(this);installEventFilter(this);
 }
 void save(){QFile file("test-results/input-receipt.json");if(file.open(QIODevice::WriteOnly))file.write(QJsonDocument(receipt).toJson());}
 bool eventFilter(QObject*,QEvent *e) override{
  if(e->type()==QEvent::MouseButtonPress){auto*m=static_cast<QMouseEvent*>(e);receipt[m->button()==Qt::RightButton?"right_click":"mouse_press"]=true;save();}
  if(e->type()==QEvent::MouseMove){auto*m=static_cast<QMouseEvent*>(e);if(m->buttons()&Qt::LeftButton){receipt["drag"]=true;save();}}
  if(e->type()==QEvent::Wheel){receipt["scroll"]=true;save();}
  if(e->type()==QEvent::KeyPress){auto*k=static_cast<QKeyEvent*>(e);receipt[QString("key_%1").arg(k->key())]=true;if(k->modifiers()&Qt::ControlModifier)receipt["ctrl"]=true;if(k->modifiers()&Qt::AltModifier)receipt["alt"]=true;if(k->modifiers()&Qt::MetaModifier)receipt["super"]=true;save();}
  return false;
 }
};
int main(int argc,char**argv){QApplication app(argc,argv);TestWindow w;w.winId();for(auto*s:app.screens())if(s->name()=="eDP-1")w.windowHandle()->setScreen(s);w.showFullScreen();w.activateWindow();QTimer::singleShot(180000,&app,&QApplication::quit);return app.exec();}
